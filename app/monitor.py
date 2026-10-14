from __future__ import annotations
import cv2
import json
import os
import re
import shutil
import threading
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from skimage.metrics import structural_similarity as ssim

import paho.mqtt.client as mqtt

from config import Area, Config, DATA_DIR, SNAPSHOT_DIR, load_config
from llm import LlmError, classify

os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = "rtsp_transport;tcp"

REF_CLOSED = str(DATA_DIR / "ref_closed.jpg")
REF_OPEN   = str(DATA_DIR / "ref_open.jpg")
CURRENT    = str(DATA_DIR / "current.jpg")
SUGGESTION = str(DATA_DIR / "suggestion.jpg")
SCORE_HISTORY = str(DATA_DIR / "score_history.json")
EVENTS = str(DATA_DIR / "events.json")

# Snapshot files we create (and are therefore allowed to serve and prune), e.g.
# 2026-10-14_18-03-22_open_ai.jpg — local time, new state, what decided it.
SNAPSHOT_RE = re.compile(r"^\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}(-\d+)?_(open|closed)_(ssim|ai|fallback)\.jpg$")
_SNAPSHOT_SOURCE = {"ssim": "ssim", "llm": "ai", "fallback": "fallback"}

# Smallest crop side we accept; SSIM's default 7×7 window needs headroom.
MIN_CROP = 16
# Seconds between score-history flushes (avoids an SD-card write every cycle).
SCORE_FLUSH_INTERVAL = 60
# Bound how long a dead camera can stall a cycle (and therefore a stop/restart).
_CAPTURE_TIMEOUT_MS = 10000

_clahe_cache: dict[float, cv2.CLAHE] = {}


# ── image helpers ──────────────────────────────────────────────────────────────

def grab_frame(rtsp_url: str):
    cap = cv2.VideoCapture(rtsp_url, cv2.CAP_FFMPEG,
                           [cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, _CAPTURE_TIMEOUT_MS,
                            cv2.CAP_PROP_READ_TIMEOUT_MSEC, _CAPTURE_TIMEOUT_MS])
    if not cap.isOpened():
        return None
    ret, frame = cap.read()
    cap.release()
    return frame if ret else None


def crop(frame, area: Area | None):
    """Cut out one area, clamped to the frame. Falls back to the full frame
    when there is no area or the box doesn't overlap the image enough."""
    if area is None or area.w == 0 or area.h == 0:
        return frame
    fh, fw = frame.shape[:2]
    x0, y0 = min(area.x, fw), min(area.y, fh)
    x1, y1 = min(area.x + area.w, fw), min(area.y + area.h, fh)
    if x1 - x0 < MIN_CROP or y1 - y0 < MIN_CROP:
        return frame
    return frame[y0:y1, x0:x1]


def preprocess(img, clip_limit: float):
    clahe = _clahe_cache.get(clip_limit)
    if clahe is None:
        clahe = _clahe_cache[clip_limit] = cv2.createCLAHE(clipLimit=clip_limit,
                                                           tileGridSize=(8, 8))
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    return clahe.apply(gray)


def compare_areas(cfg: Config) -> list[Area | None]:
    """The regions to compare; None stands for the whole frame."""
    return list(cfg.areas) or [None]


def _mtime(path: str) -> float:
    try:
        return os.path.getmtime(path)
    except OSError:
        return 0.0


def _ref_key(cfg: Config) -> tuple:
    """Changes whenever an area, the preprocessing or either reference file
    changes, so the loop picks up new references without a restart."""
    return (tuple(a.model_dump_json() for a in cfg.areas), cfg.clahe_clip_limit,
            _mtime(REF_CLOSED), _mtime(REF_OPEN))


def _load_refs(cfg: Config) -> list[tuple] | None:
    """(area, closed_ref, open_ref) per area, preprocessed and size-matched."""
    img_closed, img_open = cv2.imread(REF_CLOSED), cv2.imread(REF_OPEN)
    if img_closed is None or img_open is None:
        return None
    refs = []
    for area in compare_areas(cfg):
        rc = preprocess(crop(img_closed, area), cfg.clahe_clip_limit)
        ro = preprocess(crop(img_open, area), cfg.clahe_clip_limit)
        h, w = rc.shape
        refs.append((area, rc, cv2.resize(ro, (w, h))))
    return refs


def measure(frame, refs: list[tuple], cfg: Config) -> list[dict]:
    """SSIM of every area against both references."""
    readings = []
    for area, ref_closed, ref_open in refs:
        h, w = ref_closed.shape
        current = cv2.resize(preprocess(crop(frame, area), cfg.clahe_clip_limit), (w, h))
        sc = float(ssim(current, ref_closed))
        so = float(ssim(current, ref_open))
        threshold = area.threshold if area and area.threshold is not None else cfg.ssim_threshold
        readings.append({
            "name": area.name if area else "Full frame",
            "box": [area.x, area.y, area.w, area.h] if area and area.w and area.h else None,
            "score_closed": round(sc, 4),
            "score_open": round(so, 4),
            "threshold": threshold,
            "state": "open" if so > sc else "closed",
            "confident": max(sc, so) >= threshold,
        })
    return readings


def weakest(readings: list[dict]) -> dict:
    """The area closest to (or furthest below) its threshold — the one that
    decides whether SSIM alone can be trusted."""
    return min(readings, key=lambda r: max(r["score_closed"], r["score_open"]) - r["threshold"])


# ── persistence ────────────────────────────────────────────────────────────────

def _read_list(path: str) -> list[dict]:
    try:
        data = json.loads(Path(path).read_text())
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _write_list(path: str, items: list[dict]) -> None:
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        tmp = Path(path + ".tmp")
        tmp.write_text(json.dumps(items))
        tmp.replace(path)
    except Exception as e:
        print(f"Could not save {path}: {e}")


def _save_snapshot(frame, state: str, source: str, cfg: Config) -> str | None:
    """Store the frame that caused a state change; returns the file name."""
    if not cfg.snapshots_enabled:
        return None
    try:
        SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().astimezone().strftime("%Y-%m-%d_%H-%M-%S")
        suffix = f"_{state}_{_SNAPSHOT_SOURCE.get(source, 'ssim')}.jpg"
        name, n = stamp + suffix, 1
        while (SNAPSHOT_DIR / name).exists():
            n += 1
            name = f"{stamp}-{n}{suffix}"
        if not cv2.imwrite(str(SNAPSHOT_DIR / name), frame, [cv2.IMWRITE_JPEG_QUALITY, 90]):
            raise OSError("could not write image")
        _prune_snapshots(cfg.snapshot_keep)
        return name
    except Exception as e:
        print(f"Snapshot save error: {e}")
        return None


def _prune_snapshots(keep: int) -> None:
    """Delete our oldest snapshots beyond `keep`; other files are left alone."""
    if keep <= 0:
        return
    files = sorted((p for p in SNAPSHOT_DIR.iterdir() if SNAPSHOT_RE.match(p.name)),
                   key=lambda p: p.stat().st_mtime)
    for p in files[:-keep]:
        p.unlink(missing_ok=True)


# ── MQTT ───────────────────────────────────────────────────────────────────────

def _make_client(cfg: Config):
    try:
        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION1)
    except AttributeError:
        client = mqtt.Client()
    client.username_pw_set(cfg.mqtt_user, cfg.mqtt_password)

    # connect() only opens the socket; the broker's accept/reject verdict
    # (including auth) arrives via CONNACK, which is processed by the network
    # loop. Wait for it so bad credentials surface as a clear startup error
    # instead of silently failing on the first publish.
    done = threading.Event()
    result: dict = {}

    def on_connect(c, userdata, flags, rc, *args):
        result["rc"] = getattr(rc, "value", rc)
        done.set()

    client.on_connect = on_connect
    client.connect(cfg.mqtt_broker, cfg.mqtt_port, 5)
    client.loop_start()

    if not done.wait(timeout=5.0):
        client.loop_stop()
        client.disconnect()
        raise RuntimeError("no response from broker (CONNACK timed out)")
    rc = result.get("rc")
    if rc != 0:
        client.loop_stop()
        client.disconnect()
        raise RuntimeError(f"broker refused connection (code {rc})")
    return client


def _publish(client, topic: str, payload: str, retain: bool = False) -> None:
    try:
        client.publish(topic, payload, retain=retain).wait_for_publish(timeout=5.0)
    except Exception as e:
        print(f"MQTT publish error: {e}")


# ── shared state ───────────────────────────────────────────────────────────────

class MonitorState:
    def __init__(self):
        self._lock = threading.Lock()
        self.state: str = "unknown"
        self.source: str | None = None
        self.score_closed: float = 0.0
        self.score_open: float = 0.0
        self.areas: list[dict] = []
        self.last_update: str | None = None
        self.error: str | None = None
        self.running: bool = False
        # State-change events, persisted so the log and its snapshots survive
        # restarts.
        self.history: list[dict] = _read_list(EVENTS)
        self.llm: dict | None = None
        self.suggestion: dict | None = None
        # Rolling history of every confidence reading (not just state changes),
        # persisted to disk so the dashboard chart survives reloads/restarts.
        self.score_history: list[dict] = _read_list(SCORE_HISTORY)
        self._last_flush = time.monotonic()
        self._dirty = False

    def set(self, **fields) -> None:
        with self._lock:
            for k, v in fields.items():
                setattr(self, k, v)

    def is_new_event(self, state: str) -> bool:
        """A real change compared to the last logged event. Restarts reset the
        live state to "unknown", but must not log (and snapshot) the same state
        again."""
        with self._lock:
            return (state in ("open", "closed")
                    and (not self.history or self.history[-1]["state"] != state))

    def update(self, state: str, readings: list[dict], error: str | None = None,
               source: str = "ssim", *, frame_size: tuple[int, int] | None = None,
               snapshot: str | None = None, llm: dict | None = None,
               history_limit: int = 50) -> bool:
        """Record one reading; on a new state also log an event (with the
        deciding snapshot). Returns True if the live state changed."""
        top = weakest(readings)
        log_event = self.is_new_event(state)
        with self._lock:
            changed = state != self.state
            self.state = state
            self.source = source
            self.score_closed = top["score_closed"]
            self.score_open = top["score_open"]
            self.areas = readings
            self.last_update = datetime.now(timezone.utc).isoformat()
            self.error = error
            if log_event:
                self.history.append({
                    "ts": self.last_update,
                    "state": state,
                    "source": source,
                    "score_closed": top["score_closed"],
                    "score_open": top["score_open"],
                    "areas": readings,
                    "frame": list(frame_size) if frame_size else None,
                    "snapshot": snapshot,
                    "llm": ({k: llm.get(k) for k in ("state", "confidence", "reason", "model")}
                            if llm else None),
                    "note": error,
                })
                self.history = self.history[-history_limit:]
                events = list(self.history)
        if log_event:
            _write_list(EVENTS, events)
        return changed

    def mark_failure(self, error: str) -> None:
        with self._lock:
            self.score_closed = self.score_open = 0.0
            self.areas = []
            self.last_update = datetime.now(timezone.utc).isoformat()
            self.error = error

    def record_score(self, ts: str, readings: list[dict], limit: int) -> None:
        """Append one confidence reading; flush to disk at most once a minute.
        Top-level scores are the least confident area's; `areas` holds every
        area compactly as [name, closed, open, threshold]."""
        top = weakest(readings)
        with self._lock:
            self.score_history.append({
                "ts": ts,
                "score_closed": top["score_closed"],
                "score_open": top["score_open"],
                "areas": [[r["name"], r["score_closed"], r["score_open"], r["threshold"]]
                          for r in readings],
            })
            if limit > 0:
                self.score_history = self.score_history[-limit:]
            self._dirty = True
        if time.monotonic() - self._last_flush >= SCORE_FLUSH_INTERVAL:
            self.flush_scores()

    def flush_scores(self) -> None:
        with self._lock:
            if not self._dirty:
                return
            snapshot = list(self.score_history)
            self._dirty = False
            self._last_flush = time.monotonic()
        _write_list(SCORE_HISTORY, snapshot)

    def get_score_history(self, limit: int) -> list[dict]:
        with self._lock:
            if limit > 0:
                return list(self.score_history[-limit:])
            return list(self.score_history)

    def as_dict(self) -> dict:
        with self._lock:
            return {
                "state": self.state,
                "source": self.source,
                "score_closed": round(self.score_closed, 4),
                "score_open": round(self.score_open, 4),
                "areas": list(self.areas),
                "last_update": self.last_update,
                "error": self.error,
                "running": self.running,
                "history": list(self.history),
                "llm": self.llm,
                "suggestion": self.suggestion,
            }


monitor_state = MonitorState()

_control_lock = threading.Lock()
_thread: threading.Thread | None = None
_stop_event = threading.Event()


# ── decision ───────────────────────────────────────────────────────────────────

def _ask_llm(frame, cfg: Config) -> dict | None:
    try:
        result = classify(frame, cfg)
    except LlmError as e:
        monitor_state.set(llm={"error": str(e), "ts": _now(), "model": cfg.llm_model})
        return None
    result["ts"] = _now()
    monitor_state.set(llm=result)
    return result


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def decide(frame, readings: list[dict], cfg: Config, current_state: str,
           consecutive_unsure: int) -> tuple[str, str, str | None, int, dict | None]:
    """Pick the reported state from the per-area SSIM readings, optionally
    consulting the LLM.

    SSIM alone decides only when every area clears its threshold and all areas
    agree. Otherwise — at least one area below its threshold, areas
    disagreeing, or a close call — the reading is uncertain and the LLM is asked.

    Returns (state, source, error_note, consecutive_unsure, llm_result).
    `source` is "ssim", "llm" or "fallback" (the safety default when nothing is
    sure). Safety bias: the LLM may always turn a reading into "open", but it
    can only report "closed" when SSIM itself was unsure.
    """
    states = {r["state"] for r in readings}
    all_confident = all(r["confident"] for r in readings)
    agree = len(states) == 1
    close_call = any(abs(r["score_closed"] - r["score_open"]) < cfg.llm_margin
                     for r in readings)
    ssim_state = next(iter(states)) if agree else "open"
    res = None

    if cfg.llm_mode != "off":
        uncertain = not all_confident or not agree or close_call
        verify = (cfg.llm_mode == "verify" and ssim_state == "closed"
                  and current_state != "closed")
        if uncertain or verify:
            res = _ask_llm(frame, cfg)
            if res and res["confidence"] >= cfg.llm_min_confidence:
                if uncertain or res["state"] == "open":
                    return res["state"], "llm", None, 0, res

    if all_confident and agree:
        return ssim_state, "ssim", None, 0, res
    if any(r["confident"] and r["state"] == "open" for r in readings):
        return "open", "ssim", "Areas disagree — reporting open (safety)", 0, res

    consecutive_unsure += 1
    if consecutive_unsure >= cfg.fallback_after:
        return ("open", "fallback", "Low confidence — defaulting to open (safety)",
                consecutive_unsure, res)
    prev = current_state if current_state in ("open", "closed") else "unknown"
    return prev, "fallback", "Low confidence — keeping previous state", consecutive_unsure, res


def _track_suggestion(frame, state: str, source: str, readings: list[dict],
                      cfg: Config, streak: dict) -> None:
    """If the LLM keeps deciding because SSIM can't match *either* reference in
    some area, the reference for that state is probably stale — offer to
    replace it."""
    if source == "llm" and not all(r["confident"] for r in readings):
        streak["n"] = streak["n"] + 1 if streak.get("state") == state else 1
        streak["state"] = state
        if streak["n"] >= cfg.suggest_after and not monitor_state.suggestion:
            cv2.imwrite(SUGGESTION, frame)
            monitor_state.set(suggestion={"state": state, "ts": _now()})
    else:
        streak.clear()
        if source == "ssim" and monitor_state.suggestion:
            monitor_state.set(suggestion=None)


# ── monitoring loop ────────────────────────────────────────────────────────────

def _loop(stop: threading.Event) -> None:
    try:
        _run(stop)
    except Exception as e:
        traceback.print_exc()
        monitor_state.set(error=f"Monitor crashed: {e}")
    finally:
        monitor_state.flush_scores()
        # Only the current generation may clear the running flag; a superseded
        # thread must not stop the loop that replaced it.
        with _control_lock:
            if stop is _stop_event:
                monitor_state.set(running=False)


def _run(stop: threading.Event) -> None:
    cfg = load_config()
    ref_key = _ref_key(cfg)
    refs = _load_refs(cfg)
    if refs is None:
        monitor_state.set(error="Reference images not found — complete Setup first.")
        return

    mqtt_client = None
    if cfg.mqtt_broker:
        try:
            mqtt_client = _make_client(cfg)
        except Exception as e:
            monitor_state.set(error=f"MQTT connection failed: {e}")

    consecutive_unsure = 0
    published: str | None = None
    streak: dict = {}

    # Reset so the first confident reading always publishes
    monitor_state.set(state="unknown", source=None)

    try:
        while not stop.is_set():
            cfg = load_config()
            try:
                # Reload references if the crop box or a reference image changed
                new_key = _ref_key(cfg)
                if new_key != ref_key:
                    new_refs = _load_refs(cfg)
                    if new_refs is not None:
                        refs = new_refs
                        ref_key = new_key
                        streak.clear()
                        monitor_state.set(suggestion=None)

                if mqtt_client and not mqtt_client.is_connected():
                    try:
                        mqtt_client.reconnect()
                    except Exception as e:
                        monitor_state.set(error=f"MQTT reconnect failed: {e}")

                frame = grab_frame(cfg.rtsp_url)
                if frame is None:
                    monitor_state.mark_failure("Failed to grab frame from RTSP stream")
                    stop.wait(cfg.check_interval)
                    continue

                cv2.imwrite(CURRENT, frame)

                readings = measure(frame, refs, cfg)
                new_state, source, note, consecutive_unsure, llm_res = decide(
                    frame, readings, cfg, monitor_state.state, consecutive_unsure)
                # Only this thread logs events, so checking before update() is
                # safe; the snapshot name is then stored with the event.
                snapshot = None
                if monitor_state.is_new_event(new_state):
                    snapshot = _save_snapshot(frame, new_state, source, cfg)
                monitor_state.update(new_state, readings, note, source=source,
                                     frame_size=(frame.shape[1], frame.shape[0]),
                                     snapshot=snapshot, llm=llm_res,
                                     history_limit=cfg.event_log_size)
                _track_suggestion(frame, new_state, source, readings, cfg, streak)

                top = weakest(readings)
                if mqtt_client and new_state in ("open", "closed") and new_state != published:
                    _publish(mqtt_client, cfg.mqtt_topic, new_state, retain=True)
                    _publish(mqtt_client, cfg.mqtt_topic + "/state",
                             json.dumps({"score_closed": top["score_closed"],
                                         "score_open": top["score_open"],
                                         "source": source,
                                         "snapshot": snapshot,
                                         "areas": [{k: r[k] for k in ("name", "score_closed", "score_open")}
                                                   for r in readings]}),
                             retain=True)
                    published = new_state

                monitor_state.record_score(monitor_state.last_update, readings,
                                           cfg.score_history_size)
            except Exception as e:
                # One bad cycle (odd frame, bad crop, disk full…) must not kill
                # monitoring; report it and try again next interval.
                traceback.print_exc()
                monitor_state.set(error=f"Cycle failed: {e}")

            stop.wait(cfg.check_interval)
    finally:
        if mqtt_client:
            mqtt_client.loop_stop()
            mqtt_client.disconnect()


def start_monitor() -> None:
    global _thread, _stop_event
    with _control_lock:
        if monitor_state.running:
            return
        _stop_event = threading.Event()
        monitor_state.set(running=True, error=None)
        _thread = threading.Thread(target=_loop, args=(_stop_event,),
                                   daemon=True, name="monitor")
        _thread.start()


def stop_monitor() -> None:
    with _control_lock:
        _stop_event.set()
        monitor_state.set(running=False)
        t = _thread
    if t:
        t.join(timeout=_CAPTURE_TIMEOUT_MS / 1000 * 2 + 5)


def adopt_suggestion() -> str:
    """Replace the stale reference with the frame the LLM flagged."""
    sug = monitor_state.suggestion
    if not sug or not Path(SUGGESTION).exists():
        raise FileNotFoundError("No pending suggestion")
    target = REF_CLOSED if sug["state"] == "closed" else REF_OPEN
    shutil.copyfile(SUGGESTION, target)
    monitor_state.set(suggestion=None)
    return sug["state"]

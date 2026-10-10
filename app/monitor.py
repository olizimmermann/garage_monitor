from __future__ import annotations
import cv2
import json
import os
import shutil
import threading
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from skimage.metrics import structural_similarity as ssim

import paho.mqtt.client as mqtt

from config import Config, DATA_DIR, load_config
from llm import LlmError, classify

os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = "rtsp_transport;tcp"

REF_CLOSED = str(DATA_DIR / "ref_closed.jpg")
REF_OPEN   = str(DATA_DIR / "ref_open.jpg")
CURRENT    = str(DATA_DIR / "current.jpg")
SUGGESTION = str(DATA_DIR / "suggestion.jpg")
SCORE_HISTORY = str(DATA_DIR / "score_history.json")

# Smallest crop side we accept; SSIM's default 7×7 window needs headroom.
MIN_CROP = 16
# Seconds between score-history flushes (avoids an SD-card write every cycle).
SCORE_FLUSH_INTERVAL = 60
# Consecutive LLM-decided cycles before suggesting a reference refresh.
SUGGEST_AFTER = 3
# Bound how long a dead camera can stall a cycle (and therefore a stop/restart).
_CAPTURE_TIMEOUT_MS = 10000

_clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))


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


def crop(frame, cfg: Config):
    """Apply the configured crop, clamped to the frame. Falls back to the full
    frame when no crop is set or the box doesn't overlap the image enough."""
    if cfg.crop_w == 0 or cfg.crop_h == 0:
        return frame
    fh, fw = frame.shape[:2]
    x0, y0 = min(cfg.crop_x, fw), min(cfg.crop_y, fh)
    x1, y1 = min(cfg.crop_x + cfg.crop_w, fw), min(cfg.crop_y + cfg.crop_h, fh)
    if x1 - x0 < MIN_CROP or y1 - y0 < MIN_CROP:
        return frame
    return frame[y0:y1, x0:x1]


def preprocess(img):
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    return _clahe.apply(gray)


def load_ref(path: str, cfg: Config):
    img = cv2.imread(path)
    if img is None:
        return None
    return preprocess(crop(img, cfg))


def _mtime(path: str) -> float:
    try:
        return os.path.getmtime(path)
    except OSError:
        return 0.0


def _ref_key(cfg: Config) -> tuple:
    """Changes whenever the crop box or either reference file changes, so the
    loop picks up new references without a restart."""
    return (cfg.crop_x, cfg.crop_y, cfg.crop_w, cfg.crop_h,
            _mtime(REF_CLOSED), _mtime(REF_OPEN))


def _load_refs(cfg: Config):
    rc = load_ref(REF_CLOSED, cfg)
    ro = load_ref(REF_OPEN, cfg)
    if rc is None or ro is None:
        return None
    h, w = rc.shape
    return rc, cv2.resize(ro, (w, h))


# ── score history persistence ───────────────────────────────────────────────────

def _load_score_history() -> list[dict]:
    try:
        data = json.loads(Path(SCORE_HISTORY).read_text())
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _save_score_history(history: list[dict]) -> None:
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        tmp = Path(SCORE_HISTORY + ".tmp")
        tmp.write_text(json.dumps(history))
        tmp.replace(SCORE_HISTORY)
    except Exception as e:
        print(f"Score history save error: {e}")


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
    MAX_HISTORY = 50

    def __init__(self):
        self._lock = threading.Lock()
        self.state: str = "unknown"
        self.source: str | None = None
        self.score_closed: float = 0.0
        self.score_open: float = 0.0
        self.last_update: str | None = None
        self.error: str | None = None
        self.running: bool = False
        self.history: list[dict] = []
        self.llm: dict | None = None
        self.suggestion: dict | None = None
        # Rolling history of every confidence reading (not just state changes),
        # persisted to disk so the dashboard chart survives reloads/restarts.
        self.score_history: list[dict] = _load_score_history()
        self._last_flush = time.monotonic()
        self._dirty = False

    def set(self, **fields) -> None:
        with self._lock:
            for k, v in fields.items():
                setattr(self, k, v)

    def update(self, state: str, sc: float, so: float, error: str | None = None,
               source: str = "ssim") -> bool:
        """Returns True if state changed."""
        with self._lock:
            changed = state != self.state
            self.state = state
            self.source = source
            self.score_closed = sc
            self.score_open = so
            self.last_update = datetime.now(timezone.utc).isoformat()
            self.error = error
            if changed:
                self.history.append({
                    "ts": self.last_update,
                    "state": state,
                    "source": source,
                    "score_closed": round(sc, 4),
                    "score_open": round(so, 4),
                })
                self.history = self.history[-self.MAX_HISTORY:]
        return changed

    def record_score(self, ts: str, sc: float, so: float, limit: int) -> None:
        """Append one confidence reading; flush to disk at most once a minute."""
        with self._lock:
            self.score_history.append({
                "ts": ts,
                "score_closed": round(sc, 4),
                "score_open": round(so, 4),
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
        _save_score_history(snapshot)

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


def decide(frame, sc: float, so: float, cfg: Config, current_state: str,
           consecutive_unsure: int) -> tuple[str, str, str | None, int]:
    """Pick the reported state from SSIM scores, optionally consulting the LLM.

    Returns (state, source, error_note, consecutive_unsure). `source` is
    "ssim", "llm" or "fallback" (the safety default when nothing is sure).
    Safety bias: the LLM may always turn a reading into "open", but it can
    only report "closed" when SSIM itself was unsure.
    """
    confident = max(sc, so) >= cfg.ssim_threshold
    ssim_state = "open" if so > sc else "closed"
    close_call = abs(sc - so) < cfg.llm_margin

    if cfg.llm_mode != "off":
        uncertain = not confident or close_call
        verify = (cfg.llm_mode == "verify" and ssim_state == "closed"
                  and current_state != "closed")
        if uncertain or verify:
            res = _ask_llm(frame, cfg)
            if res and res["confidence"] >= cfg.llm_min_confidence:
                if uncertain or res["state"] == "open":
                    return res["state"], "llm", None, 0

    if confident:
        return ssim_state, "ssim", None, 0

    consecutive_unsure += 1
    if consecutive_unsure >= 2:
        return "open", "fallback", "Low confidence — defaulting to open (safety)", consecutive_unsure
    prev = current_state if current_state in ("open", "closed") else "unknown"
    return prev, "fallback", "Low confidence — keeping previous state", consecutive_unsure


def _track_suggestion(frame, state: str, source: str, sc: float, so: float,
                      cfg: Config, streak: dict) -> None:
    """If the LLM keeps deciding because SSIM can't match *either* reference,
    the reference for that state is probably stale — offer to replace it."""
    if source == "llm" and max(sc, so) < cfg.ssim_threshold:
        streak["n"] = streak["n"] + 1 if streak.get("state") == state else 1
        streak["state"] = state
        if streak["n"] >= SUGGEST_AFTER and not monitor_state.suggestion:
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
    ref_closed, ref_open = refs

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
                    refs = _load_refs(cfg)
                    if refs is not None:
                        ref_closed, ref_open = refs
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
                    monitor_state.update(monitor_state.state, 0, 0,
                                         "Failed to grab frame from RTSP stream",
                                         source=monitor_state.source or "ssim")
                    stop.wait(cfg.check_interval)
                    continue

                cv2.imwrite(CURRENT, frame)

                h, w = ref_closed.shape
                current = cv2.resize(preprocess(crop(frame, cfg)), (w, h))
                sc = float(ssim(current, ref_closed))
                so = float(ssim(current, ref_open))

                new_state, source, note, consecutive_unsure = decide(
                    frame, sc, so, cfg, monitor_state.state, consecutive_unsure)
                monitor_state.update(new_state, sc, so, note, source=source)
                _track_suggestion(frame, new_state, source, sc, so, cfg, streak)

                if mqtt_client and new_state in ("open", "closed") and new_state != published:
                    _publish(mqtt_client, cfg.mqtt_topic, new_state, retain=True)
                    _publish(mqtt_client, cfg.mqtt_topic + "/state",
                             json.dumps({"score_closed": round(sc, 4),
                                         "score_open": round(so, 4),
                                         "source": source}),
                             retain=True)
                    published = new_state

                monitor_state.record_score(monitor_state.last_update, sc, so,
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

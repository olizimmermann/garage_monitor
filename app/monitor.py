from __future__ import annotations
import cv2
import json
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from skimage.metrics import structural_similarity as ssim

import paho.mqtt.client as mqtt

from config import Config, DATA_DIR, load_config

os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = "rtsp_transport;tcp"

REF_CLOSED = str(DATA_DIR / "ref_closed.jpg")
REF_OPEN   = str(DATA_DIR / "ref_open.jpg")
CURRENT    = str(DATA_DIR / "current.jpg")
SCORE_HISTORY = str(DATA_DIR / "score_history.json")

_clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))


# ── image helpers ──────────────────────────────────────────────────────────────

def grab_frame(rtsp_url: str):
    cap = cv2.VideoCapture(rtsp_url, cv2.CAP_FFMPEG)
    if not cap.isOpened():
        return None
    ret, frame = cap.read()
    cap.release()
    return frame if ret else None


def crop(frame, cfg: Config):
    if cfg.crop_w == 0 or cfg.crop_h == 0:
        return frame
    return frame[cfg.crop_y : cfg.crop_y + cfg.crop_h,
                 cfg.crop_x : cfg.crop_x + cfg.crop_w]


def preprocess(img):
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    return _clahe.apply(gray)


def load_ref(path: str, cfg: Config):
    img = cv2.imread(path)
    if img is None:
        return None
    return preprocess(crop(img, cfg))


def _crop_key(cfg: Config) -> tuple[int, int, int, int]:
    return (cfg.crop_x, cfg.crop_y, cfg.crop_w, cfg.crop_h)


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
        Path(SCORE_HISTORY).write_text(json.dumps(history))
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
        self.score_closed: float = 0.0
        self.score_open: float = 0.0
        self.last_update: str | None = None
        self.error: str | None = None
        self.running: bool = False
        self.history: list[dict] = []
        # Rolling history of every confidence reading (not just state changes),
        # persisted to disk so the dashboard chart survives reloads/restarts.
        self.score_history: list[dict] = _load_score_history()
        self._thread: threading.Thread | None = None
        self._epoch: int = 0

    def update(self, state: str, sc: float, so: float, error: str | None = None) -> bool:
        """Returns True if state changed."""
        with self._lock:
            changed = state != self.state
            self.state = state
            self.score_closed = sc
            self.score_open = so
            self.last_update = datetime.now(timezone.utc).isoformat()
            self.error = error
            if changed:
                self.history.append({
                    "ts": self.last_update,
                    "state": state,
                    "score_closed": round(sc, 4),
                    "score_open": round(so, 4),
                })
                self.history = self.history[-self.MAX_HISTORY:]
        return changed

    def record_score(self, ts: str, sc: float, so: float, limit: int) -> None:
        """Append one confidence reading to the rolling history and persist it."""
        with self._lock:
            self.score_history.append({
                "ts": ts,
                "score_closed": round(sc, 4),
                "score_open": round(so, 4),
            })
            if limit > 0:
                self.score_history = self.score_history[-limit:]
            snapshot = list(self.score_history)
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
                "score_closed": round(self.score_closed, 4),
                "score_open": round(self.score_open, 4),
                "last_update": self.last_update,
                "error": self.error,
                "running": self.running,
                "history": list(self.history),
            }


monitor_state = MonitorState()


# ── monitoring loop ────────────────────────────────────────────────────────────

def _loop(epoch: int) -> None:
    monitor_state.running = True
    cfg = load_config()

    ref_closed = load_ref(REF_CLOSED, cfg)
    ref_open   = load_ref(REF_OPEN, cfg)
    if ref_closed is None or ref_open is None:
        monitor_state.error = "Reference images not found — complete Setup first."
        monitor_state.running = False
        return

    h, w = ref_closed.shape
    ref_open = cv2.resize(ref_open, (w, h))
    crop_key = _crop_key(cfg)

    mqtt_client = None
    if cfg.mqtt_broker:
        try:
            mqtt_client = _make_client(cfg)
        except Exception as e:
            monitor_state.error = f"MQTT connection failed: {e}"

    old_state: str | None = None
    consecutive_unsure = 0
    first_cycle = True

    # Reset persisted state so the first confident reading always publishes
    monitor_state.state = "unknown"

    while monitor_state.running and monitor_state._epoch == epoch:
        cfg = load_config()

        # Reload references if the crop box changed at runtime (via /api/crop)
        new_key = _crop_key(cfg)
        if new_key != crop_key:
            rc = load_ref(REF_CLOSED, cfg)
            ro = load_ref(REF_OPEN, cfg)
            if rc is not None and ro is not None:
                ref_closed = rc
                h, w = ref_closed.shape
                ref_open = cv2.resize(ro, (w, h))
                crop_key = new_key

        # MQTT health-check
        if mqtt_client:
            if not mqtt_client.is_connected():
                try:
                    mqtt_client.reconnect()
                except Exception as e:
                    monitor_state.error = f"MQTT reconnect failed: {e}"

        frame = grab_frame(cfg.rtsp_url)
        if frame is None:
            monitor_state.update(old_state or "unknown", 0, 0,
                                 "Failed to grab frame from RTSP stream")
            time.sleep(cfg.check_interval)
            continue

        cv2.imwrite(CURRENT, frame)

        current = cv2.resize(preprocess(crop(frame, cfg)), (w, h))
        sc = ssim(current, ref_closed)
        so = ssim(current, ref_open)

        if max(sc, so) < cfg.ssim_threshold:
            consecutive_unsure += 1
            new_state = "open" if consecutive_unsure >= 2 else (old_state or "unknown")
            changed = monitor_state.update(new_state, sc, so,
                                           f"Low confidence — defaulting to {'open (safety)' if consecutive_unsure >= 2 else 'previous state'}")
            if mqtt_client and changed and new_state == "open":
                _publish(mqtt_client, cfg.mqtt_topic, "open", retain=True)
        else:
            consecutive_unsure = 0
            new_state = "open" if so > sc else "closed"
            changed = monitor_state.update(new_state, sc, so)
            if mqtt_client and (changed or first_cycle):
                _publish(mqtt_client, cfg.mqtt_topic, new_state, retain=True)
                _publish(mqtt_client, cfg.mqtt_topic + "/state",
                         json.dumps({"score_closed": round(sc, 4), "score_open": round(so, 4)}),
                         retain=True)
            first_cycle = False

        monitor_state.record_score(monitor_state.last_update, sc, so,
                                   cfg.score_history_size)

        old_state = new_state
        time.sleep(cfg.check_interval)

    if mqtt_client:
        mqtt_client.loop_stop()
        mqtt_client.disconnect()
    # Only the current generation may clear the running flag; a superseded
    # thread that exited on the epoch check must not stop the new loop.
    if monitor_state._epoch == epoch:
        monitor_state.running = False


def start_monitor() -> None:
    if monitor_state.running:
        return
    monitor_state._epoch += 1
    t = threading.Thread(target=_loop, args=(monitor_state._epoch,),
                         daemon=True, name="monitor")
    monitor_state._thread = t
    t.start()


def stop_monitor() -> None:
    monitor_state.running = False
    if monitor_state._thread:
        monitor_state._thread.join(timeout=15)

from __future__ import annotations
import asyncio
import base64
import io
import json
import os
from pathlib import Path

import cv2
import numpy as np
from fastapi import FastAPI, File, HTTPException, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from config import Config, DATA_DIR, load_config, save_config
from monitor import (
    REF_CLOSED, REF_OPEN, CURRENT,
    _clahe, _make_client, _publish, crop, grab_frame, monitor_state, preprocess,
    start_monitor, stop_monitor,
)

app = FastAPI(title="Garage Monitor")
app.mount("/static", StaticFiles(directory="static"), name="static")


# Force the browser to revalidate the SPA and its assets on every load so code
# updates (e.g. a new app.js) take effect without a manual hard-refresh. The
# etag/last-modified still yield cheap 304s when nothing changed.
@app.middleware("http")
async def no_cache_frontend(request, call_next):
    response = await call_next(request)
    path = request.url.path
    if path == "/" or path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-cache"
    return response

_VALID_STATES = {"closed", "open"}


# ── helpers ────────────────────────────────────────────────────────────────────

def _img_to_b64(img) -> str:
    _, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 85])
    return "data:image/jpeg;base64," + base64.b64encode(buf.tobytes()).decode()


def _read_frame(path: str):
    img = cv2.imread(path)
    if img is None:
        raise HTTPException(404, f"Image not found: {path}")
    return img


# ── SPA entry ──────────────────────────────────────────────────────────────────

@app.get("/")
async def index():
    return FileResponse("static/index.html")


# ── status ─────────────────────────────────────────────────────────────────────

@app.get("/api/status")
async def get_status():
    return monitor_state.as_dict()


@app.get("/api/scores")
async def get_scores():
    """Persisted confidence-score history for seeding the dashboard chart."""
    cfg = load_config()
    return {"scores": monitor_state.get_score_history(cfg.score_history_size)}


@app.post("/api/monitor/start")
async def api_start():
    if not Path(REF_CLOSED).exists() or not Path(REF_OPEN).exists():
        raise HTTPException(400, "Reference images missing — complete Setup first.")
    start_monitor()
    return {"ok": True}


@app.post("/api/monitor/stop")
async def api_stop():
    stop_monitor()
    return {"ok": True}


# ── settings ───────────────────────────────────────────────────────────────────

@app.get("/api/settings")
async def get_settings():
    return load_config().model_dump()


@app.post("/api/settings")
async def post_settings(cfg: Config):
    save_config(cfg)
    if monitor_state.running:
        stop_monitor()
        start_monitor()
    return {"ok": True}


# ── reference images ───────────────────────────────────────────────────────────

def _ref_path(state: str) -> str:
    if state not in _VALID_STATES:
        raise HTTPException(400, "state must be 'closed' or 'open'")
    return REF_CLOSED if state == "closed" else REF_OPEN


@app.post("/api/capture/{state}")
async def capture_ref(state: str):
    path = _ref_path(state)
    cfg = load_config()
    if not cfg.rtsp_url:
        raise HTTPException(400, "RTSP URL not configured.")
    frame = grab_frame(cfg.rtsp_url)
    if frame is None:
        raise HTTPException(502, "Failed to grab frame — check RTSP URL and network.")
    cv2.imwrite(path, frame)
    return {
        "ok": True,
        "image": _img_to_b64(frame),
        "width": frame.shape[1],
        "height": frame.shape[0],
    }


@app.post("/api/upload/{state}")
async def upload_ref(state: str, file: UploadFile = File(...)):
    path = _ref_path(state)
    data = await file.read()
    arr = np.frombuffer(data, np.uint8)
    frame = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    if frame is None:
        raise HTTPException(400, "Invalid image file.")
    cv2.imwrite(path, frame)
    return {
        "ok": True,
        "image": _img_to_b64(frame),
        "width": frame.shape[1],
        "height": frame.shape[0],
    }


@app.get("/api/ref/{state}.jpg")
async def get_ref(state: str):
    path = _ref_path(state)
    if not Path(path).exists():
        raise HTTPException(404, "Reference image not set yet.")
    return FileResponse(path, media_type="image/jpeg")


@app.get("/api/ref/{state}/preview")
async def get_ref_preview(state: str):
    """Cropped + CLAHE processed preview of a reference image."""
    path = _ref_path(state)
    img = cv2.imread(path)
    if img is None:
        raise HTTPException(404, "Reference image not set yet.")
    cfg = load_config()
    processed = preprocess(crop(img, cfg))
    bgr = cv2.cvtColor(processed, cv2.COLOR_GRAY2BGR)
    _, buf = cv2.imencode(".jpg", bgr, [cv2.IMWRITE_JPEG_QUALITY, 85])
    return StreamingResponse(io.BytesIO(buf.tobytes()), media_type="image/jpeg")


# ── crop ───────────────────────────────────────────────────────────────────────

class CropPayload(Config):
    pass  # reuses same model; only crop fields matter


@app.post("/api/crop")
async def apply_crop(payload: dict):
    cfg = load_config()
    cfg.crop_x = int(payload.get("x", 0))
    cfg.crop_y = int(payload.get("y", 0))
    cfg.crop_w = int(payload.get("w", 0))
    cfg.crop_h = int(payload.get("h", 0))
    save_config(cfg)
    return {"ok": True}


# ── live frame ─────────────────────────────────────────────────────────────────

@app.get("/api/current.jpg")
async def get_current():
    if not Path(CURRENT).exists():
        raise HTTPException(404, "No frame captured yet.")
    return FileResponse(CURRENT, media_type="image/jpeg")


# ── test connections ───────────────────────────────────────────────────────────

@app.post("/api/test/rtsp")
async def test_rtsp(payload: dict):
    url = payload.get("rtsp_url", "")
    if not url:
        raise HTTPException(400, "rtsp_url required")
    frame = grab_frame(url)
    if frame is None:
        raise HTTPException(502, "Could not connect to RTSP stream.")
    return {"ok": True, "width": frame.shape[1], "height": frame.shape[0]}


@app.post("/api/test/mqtt")
async def test_mqtt(payload: dict):
    import threading
    import paho.mqtt.client as mqtt_mod
    broker = payload.get("mqtt_broker", "")
    port   = int(payload.get("mqtt_port", 1883))
    user   = payload.get("mqtt_user", "")
    pwd    = payload.get("mqtt_password", "")
    if not broker:
        raise HTTPException(400, "mqtt_broker required")

    # connect() only opens the socket; the broker's accept/reject verdict (incl.
    # auth) arrives via CONNACK, which is only processed by the network loop.
    # So we must run the loop and wait for on_connect to learn the real result.
    done = threading.Event()
    result: dict = {}

    def on_connect(client, userdata, flags, rc, *args):
        result["rc"] = getattr(rc, "value", rc)  # int (v1) or ReasonCode (v2)
        done.set()

    try:
        try:
            c = mqtt_mod.Client(mqtt_mod.CallbackAPIVersion.VERSION1)
        except AttributeError:
            c = mqtt_mod.Client()
        c.username_pw_set(user, pwd)
        c.on_connect = on_connect
        c.connect(broker, port, 5)
        c.loop_start()
        got_connack = done.wait(timeout=5.0)
        c.loop_stop()
        c.disconnect()
    except Exception as e:
        raise HTTPException(502, f"MQTT connection failed: {e}")

    if not got_connack:
        raise HTTPException(504, "No response from broker (CONNACK timed out).")
    rc = result.get("rc")
    if rc != 0:
        raise HTTPException(502, f"Broker refused connection (code {rc}).")
    return {"ok": True}


# ── Home Assistant MQTT discovery ──────────────────────────────────────────────

@app.post("/api/homeassistant/discover")
async def ha_discover(payload: dict):
    """Publish a retained MQTT Discovery config so Home Assistant auto-creates a
    garage-door binary sensor. HA listens on `<prefix>/binary_sensor/.../config`
    (prefix defaults to "homeassistant") and registers the entity on receipt."""
    # Use the values currently in the form (like the Test button) so the user can
    # register without saving first, falling back to saved config where blank.
    cfg = load_config()
    cfg.mqtt_broker   = payload.get("mqtt_broker", cfg.mqtt_broker) or ""
    cfg.mqtt_port     = int(payload.get("mqtt_port", cfg.mqtt_port) or 1883)
    cfg.mqtt_user     = payload.get("mqtt_user", cfg.mqtt_user) or ""
    cfg.mqtt_password = payload.get("mqtt_password", cfg.mqtt_password) or ""
    cfg.mqtt_topic    = payload.get("mqtt_topic", cfg.mqtt_topic) or "garage/status"

    if not cfg.mqtt_broker:
        raise HTTPException(400, "Configure an MQTT broker first.")

    prefix = (payload.get("discovery_prefix") or "homeassistant").strip("/")
    config_topic = f"{prefix}/binary_sensor/garage_monitor/door/config"
    discovery = {
        "name": "Garage Door",
        "unique_id": "garage_monitor_door",
        "state_topic": cfg.mqtt_topic,
        "payload_on": "open",
        "payload_off": "closed",
        "device_class": "garage_door",
        "device": {
            "identifiers": ["garage_monitor"],
            "name": "Garage Monitor",
            "manufacturer": "olizimmermann",
            "model": "RTSP SSIM detector",
            "configuration_url": "https://github.com/olizimmermann/garage_monitor",
        },
    }

    try:
        client = _make_client(cfg)
    except Exception as e:
        raise HTTPException(502, f"MQTT connection failed: {e}")

    try:
        # Retained so HA re-discovers the entity after a restart.
        _publish(client, config_topic, json.dumps(discovery), retain=True)
        # Seed the current state so the entity isn't "unknown" until the next cycle.
        state = monitor_state.as_dict().get("state")
        if state in ("open", "closed"):
            _publish(client, cfg.mqtt_topic, state, retain=True)
    finally:
        client.loop_stop()
        client.disconnect()

    return {"ok": True, "topic": config_topic}


# ── WebSocket (real-time push) ─────────────────────────────────────────────────

@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await ws.accept()
    try:
        while True:
            await ws.send_json(monitor_state.as_dict())
            await asyncio.sleep(2)
    except (WebSocketDisconnect, Exception):
        pass


# ── startup ────────────────────────────────────────────────────────────────────

@app.on_event("startup")
async def on_startup():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if Path(REF_CLOSED).exists() and Path(REF_OPEN).exists():
        start_monitor()

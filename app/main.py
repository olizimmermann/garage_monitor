from __future__ import annotations
import asyncio
import base64
import binascii
import hashlib
import hmac
import io
import json
import os
import secrets
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

import cv2
import numpy as np
from fastapi import FastAPI, File, HTTPException, Request, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, ValidationError

from config import Config, DATA_DIR, load_config, save_config
from llm import LlmError, classify, list_vision_models, reset_backoff
from monitor import (
    MIN_CROP, REF_CLOSED, REF_OPEN, CURRENT, SUGGESTION,
    _make_client, _publish, adopt_suggestion, crop, grab_frame, monitor_state,
    preprocess, start_monitor, stop_monitor,
)

# Optional HTTP Basic auth for the whole UI/API. Strongly recommended whenever
# the dashboard is reachable by anyone you don't fully trust: it can read the
# camera and change MQTT/camera settings.
_AUTH_USER = os.getenv("WEB_USERNAME", "")
_AUTH_PASS = os.getenv("WEB_PASSWORD", "")

# Browsers don't send Basic credentials on the WebSocket upgrade, so a
# successful HTTP login also sets this cookie, which the /ws handler accepts.
# Derived from the password: survives restarts, invalidated by a password change.
_WS_COOKIE = "gm_ws"
_WS_TOKEN = (hmac.new(_AUTH_PASS.encode(), b"garage-monitor-ws:" + _AUTH_USER.encode(),
                      hashlib.sha256).hexdigest() if _AUTH_PASS else "")

# Never sent back to the browser; the form shows this hint instead.
_SECRET_FIELDS = ("mqtt_password",)


@asynccontextmanager
async def lifespan(app: FastAPI):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if Path(REF_CLOSED).exists() and Path(REF_OPEN).exists():
        start_monitor()
    yield
    await asyncio.to_thread(stop_monitor)


app = FastAPI(title="Garage Monitor", lifespan=lifespan)
app.mount("/static", StaticFiles(directory="static"), name="static")


def _authorized(header: str | None) -> bool:
    if not _AUTH_PASS:
        return True
    if not header or not header.lower().startswith("basic "):
        return False
    try:
        user, _, pwd = base64.b64decode(header[6:]).decode().partition(":")
    except (binascii.Error, UnicodeDecodeError):
        return False
    return (secrets.compare_digest(user, _AUTH_USER)
            and secrets.compare_digest(pwd, _AUTH_PASS))


@app.middleware("http")
async def auth_and_cache(request: Request, call_next):
    if request.url.path != "/healthz" and not _authorized(request.headers.get("authorization")):
        return Response("Authentication required", status_code=401,
                        headers={"WWW-Authenticate": 'Basic realm="Garage Monitor"'})
    response = await call_next(request)
    if _AUTH_PASS and request.cookies.get(_WS_COOKIE) != _WS_TOKEN:
        response.set_cookie(_WS_COOKIE, _WS_TOKEN, httponly=True, samesite="strict",
                            secure=request.url.scheme == "https")
    # Force the browser to revalidate the SPA and its assets on every load so
    # code updates (e.g. a new app.js) take effect without a hard-refresh. The
    # etag/last-modified still yield cheap 304s when nothing changed.
    path = request.url.path
    if path == "/" or path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-cache"
    return response

_VALID_STATES = {"closed", "open"}


# ── helpers ────────────────────────────────────────────────────────────────────

def _img_to_b64(img) -> str:
    _, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 85])
    return "data:image/jpeg;base64," + base64.b64encode(buf.tobytes()).decode()


def _validation_message(e: ValidationError) -> str:
    return "; ".join(f"{'.'.join(map(str, err['loc']))}: {err['msg']}" for err in e.errors())


@app.get("/healthz")
async def healthz():
    """Unauthenticated liveness probe for the container healthcheck."""
    return {"ok": True}


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
def api_stop():
    stop_monitor()
    return {"ok": True}


# ── settings ───────────────────────────────────────────────────────────────────

@app.get("/api/settings")
async def get_settings():
    data = load_config().model_dump()
    for k in _SECRET_FIELDS:
        data[k + "_set"] = bool(data[k])
        data[k] = ""
    return data


@app.post("/api/settings")
def post_settings(payload: dict):
    """Partial update: only the fields sent are changed, so the Settings form
    can't wipe values it doesn't show (e.g. the crop box). A blank secret means
    "keep the stored one"."""
    current = load_config().model_dump()
    for k in _SECRET_FIELDS:
        if not payload.get(k):
            payload.pop(k, None)
    unknown = set(payload) - set(current)
    if unknown:
        raise HTTPException(422, f"Unknown settings: {', '.join(sorted(unknown))}")
    try:
        cfg = Config(**{**current, **payload})
    except ValidationError as e:
        raise HTTPException(422, _validation_message(e))
    save_config(cfg)
    reset_backoff()  # new URL/model deserves an immediate retry
    if monitor_state.running:
        stop_monitor()
        start_monitor()
    return {"ok": True}


# ── reference images ───────────────────────────────────────────────────────────

def _ref_path(state: str) -> str:
    if state not in _VALID_STATES:
        raise HTTPException(400, "state must be 'closed' or 'open'")
    return REF_CLOSED if state == "closed" else REF_OPEN


def _ref_response(frame) -> dict:
    return {
        "ok": True,
        "image": _img_to_b64(frame),
        "width": frame.shape[1],
        "height": frame.shape[0],
    }


@app.post("/api/capture/{state}")
def capture_ref(state: str):
    path = _ref_path(state)
    cfg = load_config()
    if not cfg.rtsp_url:
        raise HTTPException(400, "RTSP URL not configured.")
    frame = grab_frame(cfg.rtsp_url)
    if frame is None:
        raise HTTPException(502, "Failed to grab frame — check RTSP URL and network.")
    cv2.imwrite(path, frame)
    return _ref_response(frame)


@app.post("/api/upload/{state}")
async def upload_ref(state: str, file: UploadFile = File(...)):
    path = _ref_path(state)
    data = await file.read()
    frame = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
    if frame is None:
        raise HTTPException(400, "Invalid image file.")
    cv2.imwrite(path, frame)
    return _ref_response(frame)


@app.get("/api/ref/{state}.jpg")
async def get_ref(state: str):
    path = _ref_path(state)
    if not Path(path).exists():
        raise HTTPException(404, "Reference image not set yet.")
    return FileResponse(path, media_type="image/jpeg")


@app.get("/api/ref/{state}/preview")
def get_ref_preview(state: str):
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

class CropPayload(BaseModel):
    x: int = Field(0, ge=0)
    y: int = Field(0, ge=0)
    w: int = Field(0, ge=0)
    h: int = Field(0, ge=0)


@app.post("/api/crop")
def apply_crop(payload: CropPayload):
    if payload.w or payload.h:
        if payload.w < MIN_CROP or payload.h < MIN_CROP:
            raise HTTPException(422, f"Crop must be at least {MIN_CROP}×{MIN_CROP} px.")
        ref = cv2.imread(REF_CLOSED)
        if ref is not None:
            rh, rw = ref.shape[:2]
            if payload.x + payload.w > rw or payload.y + payload.h > rh:
                raise HTTPException(422, f"Crop exceeds the reference image ({rw}×{rh}).")
    cfg = load_config()
    cfg.crop_x, cfg.crop_y, cfg.crop_w, cfg.crop_h = payload.x, payload.y, payload.w, payload.h
    save_config(cfg)
    return {"ok": True}


# ── live frame ─────────────────────────────────────────────────────────────────

@app.get("/api/current.jpg")
async def get_current():
    if not Path(CURRENT).exists():
        raise HTTPException(404, "No frame captured yet.")
    return FileResponse(CURRENT, media_type="image/jpeg")


# ── test connections ───────────────────────────────────────────────────────────

def _mqtt_form_config(payload: dict) -> Config:
    """Values currently in the form (so the user can test before saving),
    falling back to the saved config where blank — notably the password,
    which the browser never receives."""
    cfg = load_config()
    cfg.mqtt_broker   = payload.get("mqtt_broker", cfg.mqtt_broker) or ""
    cfg.mqtt_port     = int(payload.get("mqtt_port") or cfg.mqtt_port or 1883)
    cfg.mqtt_user     = payload.get("mqtt_user", cfg.mqtt_user) or ""
    cfg.mqtt_password = payload.get("mqtt_password") or cfg.mqtt_password
    cfg.mqtt_topic    = payload.get("mqtt_topic") or cfg.mqtt_topic or "garage/status"
    return cfg


@app.post("/api/test/rtsp")
def test_rtsp(payload: dict):
    url = payload.get("rtsp_url", "")
    if not url:
        raise HTTPException(400, "rtsp_url required")
    frame = grab_frame(url)
    if frame is None:
        raise HTTPException(502, "Could not connect to RTSP stream.")
    return {"ok": True, "width": frame.shape[1], "height": frame.shape[0]}


@app.post("/api/test/mqtt")
def test_mqtt(payload: dict):
    cfg = _mqtt_form_config(payload)
    if not cfg.mqtt_broker:
        raise HTTPException(400, "mqtt_broker required")
    try:
        client = _make_client(cfg)
    except Exception as e:
        raise HTTPException(502, f"MQTT connection failed: {e}")
    client.loop_stop()
    client.disconnect()
    return {"ok": True}


# ── Home Assistant MQTT discovery ──────────────────────────────────────────────

@app.post("/api/homeassistant/discover")
def ha_discover(payload: dict):
    """Publish a retained MQTT Discovery config so Home Assistant auto-creates a
    garage-door binary sensor. HA listens on `<prefix>/binary_sensor/.../config`
    (prefix defaults to "homeassistant") and registers the entity on receipt."""
    cfg = _mqtt_form_config(payload)
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


# ── vision LLM (Ollama) ────────────────────────────────────────────────────────

@app.get("/api/llm/models")
def llm_models(url: str = ""):
    url = url or load_config().llm_url
    if not url:
        raise HTTPException(400, "Ollama URL required")
    try:
        return {"models": list_vision_models(url)}
    except LlmError as e:
        raise HTTPException(502, str(e))


@app.post("/api/llm/check")
def llm_check(payload: dict | None = None):
    """Ask the vision model about the latest frame right now (ignores backoff).
    Form values override the saved config so it doubles as a connection test."""
    cfg = load_config()
    payload = payload or {}
    try:
        cfg = Config(**{**cfg.model_dump(),
                        **{k: v for k, v in payload.items() if k.startswith("llm_") and v}})
    except ValidationError as e:
        raise HTTPException(422, _validation_message(e))
    frame = cv2.imread(CURRENT)
    if frame is None:
        frame = grab_frame(cfg.rtsp_url) if cfg.rtsp_url else None
    if frame is None:
        raise HTTPException(404, "No frame available — start monitoring or check the camera.")
    try:
        result = classify(frame, cfg, force=True)
    except LlmError as e:
        raise HTTPException(502, str(e))
    result["manual"] = True
    monitor_state.set(llm={**result, "ts": datetime.now(timezone.utc).isoformat()})
    return result


@app.get("/api/suggestion.jpg")
async def get_suggestion_image():
    if not monitor_state.suggestion or not Path(SUGGESTION).exists():
        raise HTTPException(404, "No pending suggestion.")
    return FileResponse(SUGGESTION, media_type="image/jpeg")


@app.post("/api/suggestion/accept")
def accept_suggestion():
    try:
        state = adopt_suggestion()
    except FileNotFoundError as e:
        raise HTTPException(404, str(e))
    return {"ok": True, "state": state}


@app.post("/api/suggestion/dismiss")
async def dismiss_suggestion():
    monitor_state.set(suggestion=None)
    return {"ok": True}


# ── WebSocket (real-time push) ─────────────────────────────────────────────────

@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    # HTTP middleware doesn't run for WebSockets. Browsers don't attach Basic
    # credentials to the upgrade request, so accept the login cookie instead.
    cookie = ws.cookies.get(_WS_COOKIE, "")
    if not (_authorized(ws.headers.get("authorization"))
            or (_WS_TOKEN and hmac.compare_digest(cookie, _WS_TOKEN))):
        await ws.close(code=1008)
        return
    await ws.accept()
    try:
        while True:
            await ws.send_json(monitor_state.as_dict())
            await asyncio.sleep(2)
    except (WebSocketDisconnect, Exception):
        pass

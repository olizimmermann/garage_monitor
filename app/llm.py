"""Optional second opinion from a local vision LLM served by Ollama.

SSIM is fast and deterministic but only knows the two reference snapshots; when
the scene drifts (new objects, lighting, IR night mode) its scores sink towards
the threshold. A small vision model can still answer the plain question "is the
door open?" in a couple of seconds, so we ask it only when SSIM is unsure.
"""
from __future__ import annotations
import base64
import json
import threading
import time
import urllib.error
import urllib.request

import cv2

from config import Config

# Context window sent with every request. It must match what the server runs
# models with: a different num_ctx forces Ollama to reload the model. 4096 is
# plenty — a 768px frame plus prompt is ~450 tokens on qwen3.5, ~1100 on qwen3-vl.
NUM_CTX = 4096

# A shared edge box typically serves one request and keeps one model loaded at a
# time, queueing everything else. Never have more than one request in flight
# from this app (monitor loop + manual "Ask AI" button), and back off after a
# failure instead of piling onto a busy or offline server.
_inflight = threading.Lock()
_BACKOFF_S = 60
_backoff_until = 0.0

# Downscale before sending: 2B vision models don't benefit from 1080p and the
# smaller payload keeps latency low on edge devices like a Jetson.
_MAX_WIDTH = 768

_PROMPT = (
    "This is a security camera image from inside a garage. Look at the large "
    "garage door. Is it open (you can see outside: daylight, street, trees, cars, "
    "or a gap between door and floor) or closed (solid door panels, no view outside)? "
    'Respond only with JSON: {"state": "open" or "closed", "confidence": 0.0-1.0, '
    '"reason": "one short sentence"}'
)

_SCHEMA = {
    "type": "object",
    "properties": {
        "state": {"type": "string", "enum": ["open", "closed"]},
        "confidence": {"type": "number"},
        "reason": {"type": "string"},
    },
    "required": ["state", "confidence", "reason"],
}


class LlmError(RuntimeError):
    pass


def backoff_remaining() -> float:
    return max(0.0, _backoff_until - time.monotonic())


def reset_backoff() -> None:
    global _backoff_until
    _backoff_until = 0.0


def _request(url: str, path: str, body: dict | None, timeout: float) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url.rstrip("/") + path, data=data,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")[:200]
        raise LlmError(f"Ollama HTTP {e.code}: {detail}") from e
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise LlmError(f"Ollama unreachable: {getattr(e, 'reason', e)}") from e


def list_vision_models(url: str, timeout: float = 5.0) -> list[dict]:
    """Installed models, flagging which accept images (Ollama >= 0.6 reports
    capabilities via /api/show)."""
    tags = _request(url, "/api/tags", None, timeout).get("models", [])
    out = []
    for m in tags:
        name = m.get("name", "")
        try:
            caps = _request(url, "/api/show", {"model": name}, timeout).get("capabilities", [])
        except LlmError:
            caps = []
        out.append({
            "name": name,
            "size": m.get("details", {}).get("parameter_size", ""),
            "vision": "vision" in caps,
        })
    out.sort(key=lambda m: (not m["vision"], m["name"]))
    return out


def classify(frame, cfg: Config, *, force: bool = False) -> dict:
    """Ask the configured model whether the door is open. The full frame is sent
    (not the SSIM crop) because outside context — daylight, the driveway — is
    exactly what the model uses to decide."""
    global _backoff_until
    if not cfg.llm_url or not cfg.llm_model:
        raise LlmError("LLM URL or model not configured")
    if not force and backoff_remaining() > 0:
        raise LlmError(f"Skipped — backing off after an error ({backoff_remaining():.0f}s left)")

    h, w = frame.shape[:2]
    if w > _MAX_WIDTH:
        frame = cv2.resize(frame, (_MAX_WIDTH, int(h * _MAX_WIDTH / w)))
    ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 85])
    if not ok:
        raise LlmError("Failed to encode frame")

    body = {
        "model": cfg.llm_model,
        "stream": False,
        "think": False,
        "format": _SCHEMA,
        "keep_alive": cfg.llm_keep_alive,
        "options": {"temperature": 0, "num_ctx": NUM_CTX},
        "messages": [{"role": "user", "content": _PROMPT,
                      "images": [base64.b64encode(buf.tobytes()).decode()]}],
    }
    # The timeout covers queueing behind other clients plus a cold model load.
    if not _inflight.acquire(timeout=cfg.llm_timeout):
        raise LlmError("Another LLM request from this app is still running")
    try:
        t0 = time.monotonic()
        resp = _request(cfg.llm_url, "/api/chat", body, cfg.llm_timeout)
        latency = time.monotonic() - t0
    except LlmError:
        _backoff_until = time.monotonic() + _BACKOFF_S
        raise
    finally:
        _inflight.release()

    content = resp.get("message", {}).get("content", "")
    try:
        parsed = json.loads(content)
        state = parsed["state"]
        confidence = float(parsed["confidence"])
    except (ValueError, KeyError, TypeError) as e:
        raise LlmError(f"Unparseable model reply: {content[:120]!r}") from e
    if state not in ("open", "closed"):
        raise LlmError(f"Unexpected state from model: {state!r}")

    return {
        "state": state,
        "confidence": round(max(0.0, min(1.0, confidence)), 3),
        "reason": str(parsed.get("reason", ""))[:300],
        "model": cfg.llm_model,
        "latency": round(latency, 2),
        "load": round(resp.get("load_duration", 0) / 1e9, 2),
        "tokens": resp.get("prompt_eval_count"),
    }

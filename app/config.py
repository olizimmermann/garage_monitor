from __future__ import annotations
import json
import os
from pathlib import Path
from typing import Literal
from pydantic import BaseModel, Field

DATA_DIR = Path(os.getenv("DATA_DIR", "."))
_CONFIG_PATH = DATA_DIR / "config.json"

LlmMode = Literal["off", "uncertain", "verify"]


class Config(BaseModel):
    rtsp_url: str = ""
    mqtt_broker: str = ""
    mqtt_port: int = Field(1883, ge=1, le=65535)
    mqtt_user: str = ""
    mqtt_password: str = ""
    mqtt_topic: str = "garage/status"
    check_interval: int = Field(10, ge=1, le=3600)
    crop_x: int = Field(0, ge=0)
    crop_y: int = Field(0, ge=0)
    crop_w: int = Field(0, ge=0)
    crop_h: int = Field(0, ge=0)
    ssim_threshold: float = Field(0.4, ge=0.0, le=1.0)
    score_history_size: int = Field(60, ge=1, le=10000)
    # Optional vision-LLM second opinion (Ollama). "uncertain" asks only when
    # SSIM can't decide; "verify" additionally double-checks every switch to
    # "closed" so a stale reference can't hide an open door.
    llm_mode: LlmMode = "off"
    llm_url: str = ""
    llm_model: str = ""
    llm_min_confidence: float = Field(0.7, ge=0.0, le=1.0)
    llm_margin: float = Field(0.05, ge=0.0, le=1.0)
    # Generous: a shared Ollama box may queue us behind other clients and then
    # cold-load the model (6–15 s on a Jetson Orin Nano for a 2B vision model).
    llm_timeout: int = Field(60, ge=1, le=600)
    # How long Ollama keeps the model in memory after our request. Keep it short
    # on a box that holds only one model, so other users aren't held up.
    llm_keep_alive: str = "5m"


def _env_defaults() -> dict:
    env = {
        "rtsp_url": "RTSP_URL", "mqtt_broker": "MQTT_BROKER", "mqtt_port": "MQTT_PORT",
        "mqtt_user": "MQTT_USER", "mqtt_password": "MQTT_PASSWORD",
        "mqtt_topic": "MQTT_TOPIC", "check_interval": "CHECK_INTERVAL",
        "crop_x": "CROP_X", "crop_y": "CROP_Y", "crop_w": "CROP_W", "crop_h": "CROP_H",
        "ssim_threshold": "SSIM_THRESHOLD", "score_history_size": "SCORE_HISTORY_SIZE",
        "llm_mode": "LLM_MODE", "llm_url": "LLM_URL", "llm_model": "LLM_MODEL",
        "llm_min_confidence": "LLM_MIN_CONFIDENCE", "llm_margin": "LLM_MARGIN",
        "llm_timeout": "LLM_TIMEOUT", "llm_keep_alive": "LLM_KEEP_ALIVE",
    }
    return {field: os.environ[var] for field, var in env.items() if os.getenv(var)}


def load_config() -> Config:
    if _CONFIG_PATH.exists():
        return Config(**json.loads(_CONFIG_PATH.read_text()))
    return Config(**_env_defaults())


def save_config(cfg: Config) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    # Write-then-rename so the monitor thread never reads a half-written file.
    tmp = _CONFIG_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(cfg.model_dump(), indent=2))
    tmp.replace(_CONFIG_PATH)

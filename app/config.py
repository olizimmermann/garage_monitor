from __future__ import annotations
import json
import os
from pathlib import Path
from pydantic import BaseModel

DATA_DIR = Path(os.getenv("DATA_DIR", "."))
_CONFIG_PATH = DATA_DIR / "config.json"


class Config(BaseModel):
    rtsp_url: str = ""
    mqtt_broker: str = ""
    mqtt_port: int = 1883
    mqtt_user: str = ""
    mqtt_password: str = ""
    mqtt_topic: str = "garage/status"
    check_interval: int = 10
    crop_x: int = 0
    crop_y: int = 0
    crop_w: int = 0
    crop_h: int = 0
    ssim_threshold: float = 0.4
    score_history_size: int = 60


def load_config() -> Config:
    if _CONFIG_PATH.exists():
        return Config(**json.loads(_CONFIG_PATH.read_text()))
    return Config(
        rtsp_url=os.getenv("RTSP_URL", ""),
        mqtt_broker=os.getenv("MQTT_BROKER", ""),
        mqtt_port=int(os.getenv("MQTT_PORT", "1883")),
        mqtt_user=os.getenv("MQTT_USER", ""),
        mqtt_password=os.getenv("MQTT_PASSWORD", ""),
        mqtt_topic=os.getenv("MQTT_TOPIC", "garage/status"),
        check_interval=int(os.getenv("CHECK_INTERVAL", "10")),
        crop_x=int(os.getenv("CROP_X", "0")),
        crop_y=int(os.getenv("CROP_Y", "0")),
        crop_w=int(os.getenv("CROP_W", "0")),
        crop_h=int(os.getenv("CROP_H", "0")),
        ssim_threshold=float(os.getenv("SSIM_THRESHOLD", "0.4")),
        score_history_size=int(os.getenv("SCORE_HISTORY_SIZE", "60")),
    )


def save_config(cfg: Config) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    _CONFIG_PATH.write_text(json.dumps(cfg.model_dump(), indent=2))

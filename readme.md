# Garage Monitor

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.12-blue.svg)](https://www.python.org/)
[![Docker](https://img.shields.io/badge/docker-ready-2496ED.svg?logo=docker&logoColor=white)](docker-compose_template.yml)
[![PRs Welcome](https://img.shields.io/badge/PRs-welcome-brightgreen.svg)](CONTRIBUTING.md)

A self-hosted web app that watches an RTSP camera and reports whether your garage
door is **open** or **closed** — no extra sensors, switches, or wiring required.
It compares the live camera frame against two reference snapshots (one of the door
closed, one open) and publishes the result to MQTT for Home Assistant or any other
home-automation platform.

A built-in web dashboard handles the whole setup: capture reference images, draw a
crop box over the door, tune the match threshold, and watch live confidence scores.

## How it works

1. You capture two reference images — the door **closed** and **open**.
2. You draw a **crop box** around the part of the frame that changes (just the door),
   so the rest of the scene doesn't affect the match.
3. On each cycle the monitor grabs a frame, crops it, applies
   [CLAHE](https://en.wikipedia.org/wiki/Adaptive_histogram_equalization#Contrast_Limited_AHE)
   contrast normalization (so lighting changes don't fool it), and computes the
   [SSIM](https://en.wikipedia.org/wiki/Structural_similarity) structural similarity
   against each reference. The higher score wins.
4. The state is published to MQTT (retained) and pushed live to the dashboard over a
   WebSocket.

**Safety-first low-confidence handling:** if neither reference matches above the
configured threshold for two consecutive cycles, the door is reported as **open**.
A garage you think is closed but isn't is the dangerous case, so ambiguity defaults
to open rather than to a false "closed".

## Features

- 📷 RTSP camera input (TCP transport) — works with most IP cameras
- 🎯 Crop-box targeting so only the door region is compared
- 💡 CLAHE contrast normalization for robustness to lighting changes
- 🔀 MQTT publishing with retained state (Home Assistant ready)
- 🏠 One-click Home Assistant registration via MQTT auto-discovery
- 📊 Live dashboard: status badge, confidence bars, score chart, event history
- 💾 Persistent confidence history — the chart survives reloads and restarts
- 🛠️ Browser-based setup — capture, upload, crop, and test connections in the UI
- 🐳 Single-container Docker deployment

## Quick start (Docker Compose)

1. Copy the compose template and create your environment file:

   ```bash
   cp docker-compose_template.yml docker-compose.yml
   ```

2. Create your `.env` from the example and fill in your camera and broker details:

   ```bash
   cp .env.example .env
   # then edit .env
   ```

   ```ini
   RTSP_URL=rtsp://user:password@192.168.1.50/live0
   MQTT_BROKER=192.168.1.10
   MQTT_PORT=1883
   MQTT_USER=mqtt_user
   MQTT_PASSWORD=changeme
   MQTT_TOPIC=garage/status
   ```

3. Build and start:

   ```bash
   docker compose up -d --build
   ```

4. Open the dashboard at **http://localhost:8088** and complete the **Setup** tab:
   capture/upload the closed and open reference images, draw the crop box, then start
   monitoring.

Settings and reference images persist in the `./data` volume, so they survive
restarts and image rebuilds.

## Configuration

All settings can be edited live in the dashboard's **Settings** tab; they're written
to `data/config.json`. On first run, the values below seed the defaults from
environment variables.

| Variable / setting | Default          | Description                                                        |
| ------------------ | ---------------- | ------------------------------------------------------------------ |
| `RTSP_URL`         | —                | Full RTSP stream URL, including credentials                        |
| `MQTT_BROKER`      | —                | MQTT broker host/IP (leave blank to disable MQTT)                  |
| `MQTT_PORT`        | `1883`           | MQTT broker port                                                   |
| `MQTT_USER`        | —                | MQTT username                                                      |
| `MQTT_PASSWORD`    | —                | MQTT password                                                      |
| `MQTT_TOPIC`       | `garage/status`  | Base topic for published state                                     |
| `CHECK_INTERVAL`   | `10`             | Seconds between frame checks                                       |
| `SSIM_THRESHOLD`   | `0.4`            | Minimum SSIM score to trust a match; below it → safety "open"      |
| `SCORE_HISTORY_SIZE` | `60`           | Confidence readings kept & plotted on the dashboard chart          |
| `CROP_X/Y/W/H`     | `0`              | Crop box; usually set by drawing it in the UI                      |
| `DATA_DIR`         | `.`              | Where config and images are stored (set to `/app/data` in Docker)  |

## MQTT topics

With the default `MQTT_TOPIC=garage/status`:

| Topic                 | Payload                                          | Retained |
| --------------------- | ------------------------------------------------ | -------- |
| `garage/status`       | `open` or `closed`                               | yes      |
| `garage/status/state` | `{"score_closed": 0.83, "score_open": 0.34}`     | yes      |

### Home Assistant

**Easiest:** in the dashboard's **Settings ▸ MQTT** section, click **Add to Home
Assistant**. This publishes a retained [MQTT discovery](https://www.home-assistant.io/integrations/mqtt/#mqtt-discovery)
message, and Home Assistant automatically creates a **Garage Door** binary sensor
under a "Garage Monitor" device — no YAML required (MQTT discovery is enabled in HA
by default).

**Manual alternative** — add it to your HA configuration yourself:

```yaml
mqtt:
  binary_sensor:
    - name: Garage Door
      state_topic: garage/status
      payload_on: open
      payload_off: closed
      device_class: garage_door
```

## API

The dashboard is a single-page app backed by a small FastAPI service:

| Method | Path                       | Purpose                              |
| ------ | -------------------------- | ------------------------------------ |
| GET    | `/api/status`              | Current state, scores, history       |
| GET    | `/api/scores`              | Persisted confidence-score history   |
| POST   | `/api/monitor/start`       | Start the monitor loop               |
| POST   | `/api/monitor/stop`        | Stop the monitor loop                |
| GET    | `/api/settings`            | Read config                          |
| POST   | `/api/settings`            | Save config (restarts monitor)       |
| POST   | `/api/capture/{state}`     | Capture a reference from the camera  |
| POST   | `/api/upload/{state}`      | Upload a reference image             |
| POST   | `/api/crop`                | Save the crop box                    |
| POST   | `/api/test/rtsp`           | Test an RTSP URL                     |
| POST   | `/api/test/mqtt`           | Test MQTT broker credentials         |
| POST   | `/api/homeassistant/discover` | Publish HA MQTT discovery config  |
| GET    | `/api/current.jpg`         | Latest captured frame                |
| WS     | `/ws`                      | Live status push (every 2 s)         |

`{state}` is `closed` or `open`.

## Local development

```bash
cd app
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt
uvicorn main:app --reload --port 8080
```

Requires `ffmpeg` and OpenCV's runtime libs (`libgl1`, `libglib2.0-0`) on the host —
see the `Dockerfile` for the exact packages.

## Contributing

Contributions are welcome! Please read [CONTRIBUTING.md](CONTRIBUTING.md) for the
development setup and pull-request workflow, and note our
[Code of Conduct](CODE_OF_CONDUCT.md). Found a security issue? See
[SECURITY.md](SECURITY.md) for how to report it privately.

## License

[MIT](LICENSE) © Oliver Zimmermann

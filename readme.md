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

A built-in web dashboard handles the whole setup: capture reference images, mark one
or more areas to compare, tune the thresholds, and watch live confidence scores.
Every state change is logged with a snapshot of the frame that caused it.
Optionally, a local vision model (via [Ollama](https://ollama.com)) gives a second
opinion when the image match is unsure.

Example dashboard view:

![Dashboard screenshot showing door status, confidence bars, score chart, and event history](./images/Garage_Monitor.png)

## How it works

1. You capture two reference images — the door **closed** and **open**.
2. You mark one or more **areas** of the frame that change with the door (e.g. the
   door panel and the gap at the floor), so the rest of the scene doesn't affect the
   match. Without areas the whole frame is compared.
3. On each cycle the monitor grabs a frame, cuts out every area, applies
   [CLAHE](https://en.wikipedia.org/wiki/Adaptive_histogram_equalization#Contrast_Limited_AHE)
   contrast normalization (so lighting changes don't fool it), and computes the
   [SSIM](https://en.wikipedia.org/wiki/Structural_similarity) structural similarity
   of each area against both references.
4. If every area clears its threshold and all areas agree, that's the state. If **at
   least one area is below its threshold** (or the areas disagree, or it's a close
   call), the reading is uncertain and the optional
   [AI second opinion](#ai-second-opinion-ollama) is consulted; otherwise the safety
   rules below apply.
5. The state is published to MQTT (retained) and pushed live to the dashboard over a
   WebSocket. On every change the deciding frame is saved as a
   [snapshot](#snapshots).

**Safety-first low-confidence handling:** if an area confidently sees the door open,
the door is reported **open** even when other areas disagree. If the areas can't
decide for two consecutive cycles (configurable), the door is reported as **open**.
A garage you think is closed but isn't is the dangerous case, so ambiguity defaults
to open rather than to a false "closed".

## Features

- 📷 RTSP camera input (TCP transport) — works with most IP cameras
- 🎯 Multiple comparison areas, each with an optional own threshold
- 📸 Snapshot of the deciding frame for every state change, viewable from the event log
- 💡 CLAHE contrast normalization for robustness to lighting changes
- 🤖 Optional local vision-LLM (Ollama) for uncertain readings, plus stale-reference detection
- 🔒 Optional HTTP Basic auth for the dashboard and API
- 🔀 MQTT publishing with retained state (Home Assistant ready)
- 🏠 One-click Home Assistant registration via MQTT auto-discovery
- 📊 Live dashboard: status badge, confidence bars, score chart, event history, and
  how each state was decided (image match, AI, or safety fallback)
- 💾 Persistent confidence history — the chart survives reloads and restarts
- 🛠️ Browser-based setup — capture, upload, crop, and test connections in the UI
- 🐳 Single-container Docker deployment, unprivileged user, built-in healthcheck

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

   # Recommended: protect the dashboard with a login
   WEB_USERNAME=admin
   WEB_PASSWORD=pick-a-strong-password
   ```

   Only `RTSP_URL` is required — MQTT, login and the
   [AI second opinion](#ai-second-opinion-ollama) are optional. See
   [`.env.example`](.env.example) for every option.

3. Build and start:

   ```bash
   docker compose up -d --build
   ```

4. Open the dashboard at **http://localhost:8088** and complete the **Setup** tab:
   capture/upload the closed and open reference images, mark the comparison areas, then
   press **Start**. From then on monitoring starts automatically with the container.

Settings and reference images persist in the `./data` volume, snapshots in the
`./snapshots` volume, so they survive restarts and image rebuilds.

**Tip:** take the reference images in typical conditions, and retake them when the
garage changes noticeably (new objects, moved camera). Tight areas around the door
itself make the match far more robust to everything else in the scene.

## Configuration

All settings can be edited live in the dashboard's **Settings** tab; they're written
to `data/config.json`. On first run, the values below seed the defaults from
environment variables.

> **Note:** environment variables are only read while `data/config.json` doesn't
> exist yet. After that, change settings in the dashboard — editing `.env` has no
> effect, except for `WEB_USERNAME` / `WEB_PASSWORD`, which are always read from
> the environment.

| Variable / setting | Default          | Description                                                        |
| ------------------ | ---------------- | ------------------------------------------------------------------ |
| `RTSP_URL`         | —                | Full RTSP stream URL, including credentials                        |
| `MQTT_BROKER`      | —                | MQTT broker host/IP (leave blank to disable MQTT)                  |
| `MQTT_PORT`        | `1883`           | MQTT broker port                                                   |
| `MQTT_USER`        | —                | MQTT username                                                      |
| `MQTT_PASSWORD`    | —                | MQTT password                                                      |
| `MQTT_TOPIC`       | `garage/status`  | Base topic for published state                                     |
| `CHECK_INTERVAL`   | `10`             | Seconds between frame checks                                       |
| `SSIM_THRESHOLD`   | `0.4`            | Minimum SSIM score to trust an area; areas can override it in Setup |
| `SCORE_HISTORY_SIZE` | `60`           | Confidence readings kept & plotted on the dashboard chart          |
| `EVENT_LOG_SIZE`   | `50`             | State changes kept in the event log                                |
| `CLAHE_CLIP_LIMIT` | `2.0`            | Strength of the lighting normalization                             |
| `FALLBACK_AFTER`   | `2`              | Unsure cycles in a row before defaulting to "open"                 |
| `SNAPSHOTS_ENABLED`| `true`           | Save the frame behind each state change                            |
| `SNAPSHOT_KEEP`    | `500`            | Newest snapshots to keep (`0` = all)                               |
| `CROP_X/Y/W/H`     | `0`              | Legacy single crop box — migrated to the first area automatically  |
| `LLM_MODE`         | `off`            | `off`, `uncertain` or `verify` — see below                         |
| `LLM_URL`          | —                | Ollama base URL, e.g. `http://192.168.1.20:11434`                  |
| `LLM_MODEL`        | —                | Vision model name, e.g. `qwen3.5:2b`                               |
| `LLM_MIN_CONFIDENCE` | `0.7`          | Ignore model answers below this self-reported confidence           |
| `LLM_MARGIN`       | `0.05`           | Also ask when the two SSIM scores are closer than this             |
| `LLM_TIMEOUT`      | `60`             | Seconds per request, incl. queueing and cold model load            |
| `LLM_KEEP_ALIVE`   | `5m`             | How long Ollama keeps the model loaded after a request             |
| `LLM_BACKOFF`      | `60`             | Seconds to pause AI requests after a failure                       |
| `SUGGEST_AFTER`    | `3`              | AI-decided cycles before offering a new reference                  |

These are **environment-only** (not stored in `config.json`):

| Variable           | Default               | Description                                                  |
| ------------------ | --------------------- | ------------------------------------------------------------ |
| `WEB_USERNAME`     | —                     | Username for HTTP Basic auth on the dashboard/API            |
| `WEB_PASSWORD`     | —                     | Enables Basic auth when set. Recommended on shared networks. |
| `DATA_DIR`         | `.`                   | Where config and references are stored (`/app/data` in Docker) |
| `SNAPSHOT_DIR`     | `$DATA_DIR/snapshots` | Where snapshots are stored (`/app/snapshots` in Docker)      |
| `TZ`               | `UTC`                 | Time zone used in snapshot file names                        |

The MQTT password is never sent back to the browser; leave the field blank in
Settings to keep the stored one.

## AI second opinion (Ollama)

SSIM only knows your two reference snapshots. When the scene drifts — new stuff in
the garage, a different light, IR night mode — both scores sink and the monitor
falls back to the safety default. A small local vision model can still answer the
plain question *"is the door open?"*, so Garage Monitor can ask one.

1. Run [Ollama](https://ollama.com) somewhere on your network with a
   vision-capable model, e.g. `ollama pull qwen3.5:2b`.
2. In **Settings ▸ AI Second Opinion**, enter the Ollama URL, click **Load models**
   (only vision models are selectable), pick a mode, and **Test on current frame**.

| Mode        | When the model is asked                                                              |
| ----------- | ------------------------------------------------------------------------------------ |
| `uncertain` | Only when at least one area is below its threshold, the areas disagree, or an area's two scores are within `LLM_MARGIN`. |
| `verify`    | As above, **plus** before every switch to *closed*, so a stale reference can't hide an open door. |

**Safety rules.** The model may always turn a reading into *open*, but it only
reports *closed* when SSIM itself was unsure. Answers below `LLM_MIN_CONFIDENCE`
are ignored. If Ollama is unreachable or times out, the monitor carries on with
SSIM and the usual safety fallback, and pauses AI requests for `LLM_BACKOFF` seconds.

**Stale-reference suggestions.** If an area matches neither reference but the model
keeps seeing the same state (3 cycles in a row by default), the dashboard offers that frame as the new
reference. Check the picture, then accept or dismiss — nothing is replaced
automatically.

**Shared / edge servers.** The app sends at most one request at a time, always uses
a 4096-token context (a different `num_ctx` would force Ollama to reload the
model), and downsizes frames to 768 px wide. On a Jetson Orin Nano Super running
one model at a time, measured on real frames:

| Model                  | Prompt tokens | Warm  | Cold (incl. load) |
| ---------------------- | ------------- | ----- | ----------------- |
| `qwen3.5:2b` (Q4_K_M)  | ~440          | ~2.2 s | 6–9 s            |
| `qwen3-vl:2b-instruct` | ~1130         | ~4.5 s | 10–15 s          |

Both classified all test frames correctly (day/night, open/closed, half-open);
`qwen3.5:2b` is the recommended default. The published `…/state` MQTT payload
includes a `source` field (`ssim`, `llm` or `fallback`) so automations can tell
how a state was decided.

## MQTT topics

With the default `MQTT_TOPIC=garage/status`:

| Topic                 | Payload                                          | Retained |
| --------------------- | ------------------------------------------------ | -------- |
| `garage/status`       | `open` or `closed`                               | yes      |
| `garage/status/state` | `{"score_closed": 0.83, "score_open": 0.34, "source": "ssim", "snapshot": "2026-10-14_18-41-07_closed_ssim.jpg", "areas": [{"name": "Door", "score_closed": 0.83, "score_open": 0.34}]}` | yes |

The top-level scores are those of the least confident area.

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

## Snapshots

Whenever the reported state changes, the camera frame that caused it is saved to
`SNAPSHOT_DIR` (`./snapshots` with the compose template — mount it wherever you
like, e.g. a NAS share). Names sort chronologically and say what happened:

```
2026-10-14_18-03-22_open_ai.jpg        # local time (TZ), new state, decided by the AI
2026-10-14_18-41-07_closed_ssim.jpg    # decided by the image match
2026-10-14_19-02-55_open_fallback.jpg  # safety fallback
```

Click an entry in the dashboard's **Event Log** to see its snapshot with the compared
areas outlined, each area's scores and threshold, and the AI's reasoning if it was
consulted. Only the newest `SNAPSHOT_KEEP` files are kept; other files in the folder
are never touched.

## API

The dashboard is a single-page app backed by a small FastAPI service. When login is
enabled, every endpoint except `/healthz` requires HTTP Basic auth (scripts can
send it with `curl -u user:pass …`):

| Method | Path                       | Purpose                              |
| ------ | -------------------------- | ------------------------------------ |
| GET    | `/api/status`              | Current state, scores, history       |
| GET    | `/api/scores`              | Persisted confidence-score history   |
| POST   | `/api/monitor/start`       | Start the monitor loop               |
| POST   | `/api/monitor/stop`        | Stop the monitor loop                |
| GET    | `/api/settings`            | Read config                          |
| POST   | `/api/settings`            | Update config — partial, validated (restarts monitor) |
| POST   | `/api/capture/{state}`     | Capture a reference from the camera  |
| POST   | `/api/upload/{state}`      | Upload a reference image             |
| POST   | `/api/areas`               | Save the comparison areas            |
| GET    | `/api/snapshots/{name}`    | Snapshot saved with an event         |
| POST   | `/api/test/rtsp`           | Test an RTSP URL                     |
| POST   | `/api/test/mqtt`           | Test MQTT broker credentials         |
| POST   | `/api/homeassistant/discover` | Publish HA MQTT discovery config  |
| GET    | `/api/current.jpg`         | Latest captured frame                |
| GET    | `/api/llm/models`          | List Ollama models (vision flagged)  |
| POST   | `/api/llm/check`           | Ask the vision model about the latest frame |
| GET    | `/api/suggestion.jpg`      | Frame proposed as a new reference    |
| POST   | `/api/suggestion/accept`   | Adopt it as the reference            |
| POST   | `/api/suggestion/dismiss`  | Discard the suggestion               |
| GET    | `/healthz`                 | Liveness probe (no auth)             |
| WS     | `/ws`                      | Live status push (every 2 s)         |

`{state}` is `closed` or `open`.

## Security

- **Login is off by default.** Anyone who can reach the port can view the camera and
  change settings. Set `WEB_USERNAME` / `WEB_PASSWORD` and keep the dashboard on a
  trusted network; use a VPN or a TLS reverse proxy for remote access — never expose
  the port directly to the internet.
- Secrets live in `.env` and `data/config.json`, both git-ignored. The MQTT password
  is never sent to the browser.
- With the AI second opinion enabled, camera frames are sent to your Ollama server.
  Only point it at a machine you control.

See [SECURITY.md](SECURITY.md) for details and how to report a vulnerability.

## Upgrading

```bash
git pull
docker compose up -d --build
```

Your settings and reference images in `./data` are kept. Since the new AI settings
default to *off*, existing installs behave exactly as before until you enable them.
A crop box from an older version becomes the first comparison area automatically.

To keep snapshots outside `./data`, add the `./snapshots` volume and `SNAPSHOT_DIR`
from [`docker-compose_template.yml`](docker-compose_template.yml) to your
`docker-compose.yml`; without them snapshots go to `./data/snapshots`.

Older versions ran the container as root. The container now runs as an unprivileged
user and fixes the ownership of `./data` automatically on start, so files there will
show up as owned by uid `10001` on the host.

## Troubleshooting

| Symptom | Likely cause / fix |
| ------- | ------------------ |
| Dashboard loads but never updates, **Start** seems to do nothing | The live-status WebSocket (`/ws`) is blocked. Behind a reverse proxy, enable WebSocket forwarding (`Upgrade`/`Connection` headers). Reload the page after logging in. |
| "Failed to grab frame from RTSP stream" | Check the URL with **Settings ▸ Test**. The app uses RTSP over TCP; some cameras need a sub-stream path. |
| Door often reported *open* via "safety fallback" | Neither reference matches well. Retake references, tighten the crop, or enable the AI second opinion. |
| Score history or settings not saved | `./data` isn't writable — check the volume mount. |
| Event log says "Snapshot … is no longer available" | It was pruned (`SNAPSHOT_KEEP`) or deleted, or `./snapshots` isn't mounted and the container was recreated. |
| AI card shows "Ollama unreachable" | Check the URL from the container's point of view (not `localhost`), and that Ollama listens on the network (`OLLAMA_HOST=0.0.0.0`). |

## Local development

```bash
cd app
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt
uvicorn main:app --reload --port 8080
```

Requires `ffmpeg` and OpenCV's runtime libs (`libgl1`, `libglib2.0-0`) on the host —
see the `Dockerfile` for the exact packages. Data is stored in the current directory
unless you set `DATA_DIR`.

## ⭐ Like this project?

If this saved you time, please consider giving it a star on GitHub — it helps others find the project and motivates further development!

[![Star on GitHub](https://img.shields.io/github/stars/olizimmermann/garage_monitor?style=social)](https://github.com/olizimmermann/garage_monitor)

## Contributing

Contributions are welcome! Please read [CONTRIBUTING.md](CONTRIBUTING.md) for the
development setup and pull-request workflow, and note our
[Code of Conduct](CODE_OF_CONDUCT.md). Found a security issue? See
[SECURITY.md](SECURITY.md) for how to report it privately.

## License

[MIT](LICENSE) © Oliver Zimmermann

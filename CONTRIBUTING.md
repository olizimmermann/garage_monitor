# Contributing to Garage Monitor

Thanks for taking the time to contribute! 🎉 Bug reports, feature ideas, docs
fixes, and pull requests are all welcome.

## Ways to help

- **Report a bug** — open an issue using the bug-report template.
- **Suggest a feature** — open an issue using the feature-request template.
- **Send a pull request** — see the workflow below.

## Development setup

```bash
git clone https://github.com/olizimmermann/garage_monitor.git
cd garage_monitor/app
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt
uvicorn main:app --reload --port 8080
```

This needs `ffmpeg` and OpenCV's runtime libs (`libgl1`, `libglib2.0-0`) on the
host — see the `Dockerfile` for the exact packages. You can also run the whole
thing in Docker with `docker compose up -d --build`.

The app is a small FastAPI service (`app/main.py`, `app/monitor.py`,
`app/config.py`) serving a vanilla-JS single-page dashboard from `app/static/`.

## Pull request workflow

1. Fork the repo and create a branch from `main` (e.g. `fix/crop-resize`).
2. Make your change. Keep it focused — one logical change per PR.
3. Match the existing style: no build step, no framework on the frontend; keep
   the Python dependency-light.
4. Test your change against a real (or simulated) RTSP stream where relevant, and
   confirm the dashboard still loads and the monitor runs.
5. Update the README / `.env.example` if you add or rename a setting.
6. Open the PR using the template and describe what you changed and why.

## Reporting security issues

Please **don't** open public issues for security problems — see
[SECURITY.md](SECURITY.md) for how to report privately.

## Code of conduct

By participating you agree to abide by our
[Code of Conduct](CODE_OF_CONDUCT.md).

# Security Policy

## Reporting a vulnerability

Please report security vulnerabilities **privately** — do not open a public
issue.

Email **security@ozimmermann.com**, or use GitHub's
[private vulnerability reporting](https://github.com/olizimmermann/garage_monitor/security/advisories/new)
(the **Security ▸ Report a vulnerability** button on the repository). You'll get
an acknowledgement as soon as possible, and we'll work with you on a fix and
coordinated disclosure.

## Scope & hardening notes

Garage Monitor is intended to run on a **trusted local network**. Keep this in
mind when deploying:

- Authentication is **off by default**. Set `WEB_USERNAME` / `WEB_PASSWORD` to
  enable HTTP Basic auth for the dashboard, API and WebSocket. Basic auth sends
  credentials with every request, so use it over TLS (reverse proxy) or a trusted
  LAN only. Never expose the port directly to the internet; use a VPN or a
  reverse proxy with TLS.
- The MQTT password is never returned by the API. The RTSP URL (which may embed
  camera credentials) is visible to anyone who can log in.
- If you enable the AI second opinion, camera frames are sent to the configured
  Ollama server. Point it only at a server you control.
- RTSP and MQTT **credentials** are stored in `data/config.json` and read from
  `.env`. Both are git-ignored — never commit them. Restrict file permissions on
  the `data/` directory.
- The container does not need privileged access or host networking. The server
  process runs as an unprivileged user (uid 10001); the entrypoint only uses root
  to fix ownership of the `data/` volume before dropping privileges.

## Supported versions

This is a small hobby project; security fixes are applied to the latest `main`.

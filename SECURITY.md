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

- The dashboard and API have **no authentication** — anyone who can reach the
  port can view the feed and change settings. Do not expose it directly to the
  internet; put it behind a reverse proxy with auth/TLS, or a VPN.
- RTSP and MQTT **credentials** are stored in `data/config.json` and read from
  `.env`. Both are git-ignored — never commit them. Restrict file permissions on
  the `data/` directory.
- The container does not need privileged access or host networking.

## Supported versions

This is a small hobby project; security fixes are applied to the latest `main`.

#!/bin/sh
# Start as root only long enough to make the data volume writable for the
# unprivileged "app" user (older versions ran as root and left root-owned
# files behind), then drop privileges for the actual server.
set -e

DATA_DIR="${DATA_DIR:-/app/data}"
SNAPSHOT_DIR="${SNAPSHOT_DIR:-$DATA_DIR/snapshots}"
mkdir -p "$DATA_DIR" "$SNAPSHOT_DIR"

if [ "$(id -u)" = "0" ]; then
    chown -R app:app "$DATA_DIR" "$SNAPSHOT_DIR"
    exec setpriv --reuid=app --regid=app --init-groups "$@"
fi

exec "$@"

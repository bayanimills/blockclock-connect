#!/bin/sh
# BlockClock Connect entrypoint: make sure the data volume is usable, then run.
set -e
: "${DATA_DIR:=/data}"
mkdir -p "$DATA_DIR" 2>/dev/null || true
if [ ! -w "$DATA_DIR" ]; then
    echo "WARNING: $DATA_DIR is not writable by $(id -u); config will not persist" >&2
fi
exec python /app/app.py

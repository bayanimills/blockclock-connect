# BlockClock Connect - single lean image, pure python, arch-neutral
# (builds unchanged for linux/arm64 = Umbrel and linux/amd64).
FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY clock.py store.py discovery.py feeder.py app.py entrypoint.sh ./
COPY sources/ ./sources/
COPY static/ ./static/
RUN chmod +x /app/entrypoint.sh

ENV DATA_DIR=/data \
    PYTHONUNBUFFERED=1

EXPOSE 4200

# The container must not need root; Umbrel runs it as 1000:1000.
USER 1000:1000

# Graceful stop matters: on SIGTERM the app re-enables the clock's own
# rotation before exiting. Give it a moment.
STOPSIGNAL SIGTERM

CMD ["/app/entrypoint.sh"]

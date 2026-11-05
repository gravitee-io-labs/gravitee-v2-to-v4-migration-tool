# syntax=docker/dockerfile:1
FROM python:3.12-slim

# No external dependencies - the migration script and the web UI use the stdlib only
WORKDIR /app

COPY gravitee_v4_migration.py web_app.py ./
COPY static/ ./static/

# /data is the shared volume for CSVs, logs and run history
RUN mkdir -p /data/runs

ENV PYTHONUNBUFFERED=1 \
    WEB_PORT=8080 \
    DATA_DIR=/data

EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=3s CMD python3 -c "import urllib.request,os;urllib.request.urlopen(f'http://127.0.0.1:{os.environ[\"WEB_PORT\"]}/healthz')" || exit 1

# Default: web interface. The CLI is still available:
#   docker compose run --rm generate   /   docker compose run --rm migrate
CMD ["python3", "web_app.py"]

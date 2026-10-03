# Backend image. Used by the 'full' compose profile and by the Render
# deployment (render.yaml); day-to-day development runs the API on the host
# with 'make dev'.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app/backend

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md ./
COPY backend ./backend
COPY synthetic-data ./synthetic-data
COPY scripts ./scripts

RUN pip install --no-cache-dir -e ".[db]"

EXPOSE 8000
HEALTHCHECK --interval=15s --timeout=5s --start-period=20s \
    CMD curl -fsS "http://localhost:${PORT:-8000}/health" || exit 1

# The host chooses the port (Render sets PORT). Proxy headers are trusted
# because every host this runs on terminates TLS in front of it: without them
# the app believes it is serving plain http, and the voice socket's wss://
# origin no longer matches.
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000} --app-dir backend --proxy-headers --forwarded-allow-ips='*'"]

# ── Factor II  – Dependencies: use a pinned base image ───────────────────────
# python:3.12-slim is smaller and has fewer CVEs than 3.11-slim
FROM python:3.12-slim

# ── Factor XI – Logs: don't buffer Python stdout/stderr ──────────────────────
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# Install curl (used by get-servers.py) and clean apt cache in one layer
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# ── Factor II – Dependencies: copy lockfile / manifest first for layer cache ──
COPY pyproject.toml ./

# Install only production deps (no dev extras)
RUN pip install --no-cache-dir \
        flask==3.0.3 \
        speedtest-cli==2.1.3 \
        gunicorn==22.0.0

# ── Application code ──────────────────────────────────────────────────────────
COPY app.py config.py logging_config.py gunicorn.conf.py ./
COPY templates/ ./templates/

# ── Factor IV – Backing services: db.json is supplied at runtime ──────────────
# Do NOT bake db.json into the image; mount it as a volume.
# Results directory is also a volume mount (see docker-compose.yml).
RUN mkdir -p results

# ── Factor VII – Port binding: expose default, overridable via APP_PORT ───────
EXPOSE 5000

# ── Factor IX – Disposability: graceful shutdown via gunicorn config ──────────
# gunicorn.conf.py reads all tuning from env vars (GUNICORN_WORKERS, etc.)
CMD ["gunicorn", "--config", "gunicorn.conf.py", "app:app"]

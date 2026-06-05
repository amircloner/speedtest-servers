FROM python:3.11-slim

# Install curl (needed by get-servers.py) and clean up
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy dependency declaration first for layer caching
COPY pyproject.toml ./

# Install Python deps (Flask + speedtest-cli)
RUN pip install --no-cache-dir \
        flask==3.0.3 \
        speedtest-cli==2.1.3 \
        gunicorn==22.0.0

# Copy application files
COPY app.py ./
COPY templates/ ./templates/
COPY db.json ./

# Results are persisted on a volume mount
RUN mkdir -p results

EXPOSE 5000

CMD ["gunicorn", "--bind", "0.0.0.0:5000", "--workers", "1", "--threads", "8", "--timeout", "300", "app:app"]

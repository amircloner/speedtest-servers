"""
Factor III – Config
All configuration is read from environment variables with sensible defaults.
Never hardcode environment-specific values.
"""

import os
from pathlib import Path


# ── Application ───────────────────────────────────────────────────────────────
APP_HOST: str = os.environ.get("APP_HOST", "0.0.0.0")
APP_PORT: int = int(os.environ.get("APP_PORT", "5000"))
DEBUG: bool = os.environ.get("DEBUG", "false").lower() in ("1", "true", "yes")
SECRET_KEY: str = os.environ.get("SECRET_KEY", "change-me-in-production")

# ── Paths / Backing services (Factor IV) ─────────────────────────────────────
# Treat local files like backing services – configurable, not hardcoded.
DB_FILE: Path = Path(os.environ.get("DB_FILE", "db.json"))
RESULTS_DIR: Path = Path(os.environ.get("RESULTS_DIR", "results"))
CITY_PAIRS_FILE: Path = Path(os.environ.get("CITY_PAIRS_FILE", "country_city_pairs.csv"))

# ── Speedtest ─────────────────────────────────────────────────────────────────
SPEEDTEST_CONCURRENCY: int = int(os.environ.get("SPEEDTEST_CONCURRENCY", "10"))
# seconds to sleep between individual server tests (avoids rate-limiting)
SPEEDTEST_INTER_TEST_DELAY: float = float(os.environ.get("SPEEDTEST_INTER_TEST_DELAY", "1"))
# seconds to sleep between batch fetches in get-servers.py
FETCH_INTER_BATCH_DELAY: float = float(os.environ.get("FETCH_INTER_BATCH_DELAY", "2"))
# curl --max-time for each speedtest.net API request
FETCH_TIMEOUT: int = int(os.environ.get("FETCH_TIMEOUT", "30"))
FETCH_RETRIES: int = int(os.environ.get("FETCH_RETRIES", "3"))

# ── SSE stream cleanup ────────────────────────────────────────────────────────
# seconds to keep a finished job's queue in memory
STREAM_TTL: int = int(os.environ.get("STREAM_TTL", "300"))
# timeout when waiting for next queue message (triggers keepalive ping)
STREAM_POLL_TIMEOUT: int = int(os.environ.get("STREAM_POLL_TIMEOUT", "30"))

# ── Gunicorn (Factor VIII – Concurrency) ─────────────────────────────────────
# These are read directly by gunicorn.conf.py
GUNICORN_WORKERS: int = int(os.environ.get("GUNICORN_WORKERS", "1"))
GUNICORN_THREADS: int = int(os.environ.get("GUNICORN_THREADS", "8"))
GUNICORN_TIMEOUT: int = int(os.environ.get("GUNICORN_TIMEOUT", "300"))

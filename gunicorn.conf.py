"""
Gunicorn configuration file.
Factor VIII  – Concurrency: scale out via process model.
Factor IX    – Disposability: fast startup, graceful shutdown.
Factor XI    – Logs: write to stdout; log aggregator handles the rest.
Values are driven by environment variables defined in config.py.
"""

import os

# ── Binding (Factor VII – Port binding) ──────────────────────────────────────
host = os.environ.get("APP_HOST", "0.0.0.0")
port = os.environ.get("APP_PORT", "5000")
bind = f"{host}:{port}"

# ── Concurrency (Factor VIII) ─────────────────────────────────────────────────
workers = int(os.environ.get("GUNICORN_WORKERS", "1"))
threads = int(os.environ.get("GUNICORN_THREADS", "8"))
worker_class = "gthread"

# ── Timeouts (Factor IX – Disposability) ─────────────────────────────────────
timeout = int(os.environ.get("GUNICORN_TIMEOUT", "300"))
graceful_timeout = int(os.environ.get("GUNICORN_GRACEFUL_TIMEOUT", "30"))
keepalive = 5

# ── Logs (Factor XI) ──────────────────────────────────────────────────────────
# Log to stdout/stderr so the platform's log aggregator picks them up.
accesslog = "-"   # stdout
errorlog = "-"    # stderr
loglevel = os.environ.get("LOG_LEVEL", "info")
access_log_format = '%(h)s %(l)s %(u)s %(t)s "%(r)s" %(s)s %(b)s "%(f)s" "%(a)s" %(D)sµs'

"""
Factor XI – Logs
Treat logs as event streams. All log output goes to stdout/stderr.
No log files, no log rotation – the platform handles aggregation.
"""

import logging
import os
import sys


def configure_logging() -> None:
    """Configure root logger to write structured lines to stdout."""
    level_name = os.environ.get("LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)

    fmt = "%(asctime)s %(levelname)-8s [%(name)s] %(message)s"
    date_fmt = "%Y-%m-%dT%H:%M:%S%z"

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter(fmt, datefmt=date_fmt))

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)

    # quieten noisy third-party loggers
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("speedtest").setLevel(logging.WARNING)

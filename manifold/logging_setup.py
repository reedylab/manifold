"""Logging configuration."""

import logging
import os
from logging.handlers import RotatingFileHandler

LOG_PATH = os.getenv("LOG_DIR", "/app/logs")
LOG_FILE = os.path.join(LOG_PATH, "manifold.log")


def setup_logging():
    os.makedirs(LOG_PATH, exist_ok=True)

    root = logging.getLogger()
    root.setLevel(logging.INFO)

    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")

    # Console
    ch = logging.StreamHandler()
    ch.setFormatter(fmt)
    root.addHandler(ch)

    # File
    fh = RotatingFileHandler(LOG_FILE, maxBytes=5_000_000, backupCount=3)
    fh.setFormatter(fmt)
    root.addHandler(fh)

    logging.getLogger("apscheduler").setLevel(logging.WARNING)

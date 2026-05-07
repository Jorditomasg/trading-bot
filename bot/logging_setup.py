"""Centralized logging setup shared by main.py (bot) and dashboard/app.py.

Idempotent — repeated calls are no-ops, so it's safe to invoke from
Streamlit's app.py (which re-runs the script on every user interaction).
"""

from __future__ import annotations

import logging
import logging.handlers
import sys
import threading
import time
from pathlib import Path

_FMT = "%(asctime)s  %(levelname)-8s  %(name)-30s  %(message)s"
_DATEFMT = "%Y-%m-%d %H:%M:%S"

_configured = False


def setup_logging(
    level: str,
    log_dir: Path = Path("logs"),
    main_log: str = "bot.log",
    error_log: str = "errors.log",
) -> None:
    """Wire stdout + main_log + error_log handlers plus uncaught-exception hooks.

    - main_log captures everything at `level` and above.
    - error_log is WARNING+ only, rotated at 5 MB × 5 files.
    - Uncaught exceptions in the main thread and daemon threads are logged.

    `force=True` replaces any pre-existing root handlers — Streamlit installs
    its own at import time, so without it our file handlers would be ignored.
    """
    global _configured
    if _configured:
        return

    log_dir.mkdir(exist_ok=True)

    error_handler = logging.handlers.RotatingFileHandler(
        log_dir / error_log,
        maxBytes=5 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    error_handler.setLevel(logging.WARNING)
    error_handler.setFormatter(logging.Formatter(_FMT, datefmt=_DATEFMT))

    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format=_FMT,
        datefmt=_DATEFMT,
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(log_dir / main_log, encoding="utf-8"),
            error_handler,
        ],
        force=True,
    )
    logging.Formatter.converter = time.localtime

    def _log_uncaught(exc_type, exc_value, exc_tb):
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc_value, exc_tb)
            return
        logging.getLogger("uncaught").critical(
            "Unhandled exception", exc_info=(exc_type, exc_value, exc_tb),
        )

    def _log_thread_uncaught(args: threading.ExceptHookArgs) -> None:
        if issubclass(args.exc_type, SystemExit):
            return
        logging.getLogger("uncaught.thread").critical(
            "Unhandled exception in thread %s",
            args.thread.name if args.thread else "unknown",
            exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
        )

    sys.excepthook = _log_uncaught
    threading.excepthook = _log_thread_uncaught

    _configured = True

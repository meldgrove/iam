"""Whether the worker is getting anywhere.

The worker loop counts as progress each round and after each job, and so
does every answer from a connected app (iam.adapters.base), so a long read
of a big directory is progress too. A worker without any for
WORKER_STALL_SECONDS is stuck, for example on a database connection whose
server went away: the watchdog then ends the process, and the container's
restart policy starts a fresh one. A job it was running goes back to the
queue once its lease runs out.

While the worker makes progress, the watchdog touches ALIVE_FILE for the
container's health check (compose.yaml).
"""

from __future__ import annotations

import faulthandler
import logging
import os
import tempfile
import threading
import time
from pathlib import Path

from django.conf import settings

logger = logging.getLogger("iam.worker")

ALIVE_FILE = Path(tempfile.gettempdir()) / "iam-worker-alive"
WATCH_SECONDS = 10

_last_progress = time.monotonic()


def note_progress(*_args) -> None:
    global _last_progress
    _last_progress = time.monotonic()


def seconds_without_progress() -> float:
    return time.monotonic() - _last_progress


def watch(stall_seconds: float, *, alive_file: Path = ALIVE_FILE, stop=os._exit) -> bool:
    """One look of the watchdog. Ends the process when the worker is stuck;
    returns whether it is alive."""
    stalled = seconds_without_progress()
    if stalled > stall_seconds:
        logger.critical(
            "The worker made no progress for %d seconds and stops, so that it is restarted. "
            "Where each thread was:",
            stalled,
        )
        faulthandler.dump_traceback(all_threads=True)
        if settings.SENTRY_DSN:
            import sentry_sdk

            sentry_sdk.flush(timeout=5)
        stop(1)
        return False
    try:
        alive_file.touch()
    except OSError:
        logger.warning("Could not touch %s for the health check", alive_file)
    return True


def start_watchdog(stall_seconds: float) -> threading.Thread:
    note_progress()

    def run():
        while watch(stall_seconds):
            time.sleep(WATCH_SECONDS)

    thread = threading.Thread(target=run, name="worker-watchdog", daemon=True)
    thread.start()
    return thread

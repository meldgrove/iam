"""The worker: runs queued jobs and, once a minute, the schedule and one due
read from a connection.

One worker is enough for most organizations; more can run side by side,
because each job is claimed with a row lock, one worker at a time runs the
schedule and one at a time reads from connections. Stop it with SIGTERM: it
finishes the job it is running first. A watchdog ends a worker that is stuck
(iam.liveness), so that its container restarts it."""

import logging
import signal
import time

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import close_old_connections
from django.utils import timezone

from iam import liveness
from iam.jobs import claim_next_job, recover_stale_jobs, run_job
from iam.models import WorkerHeartbeat
from iam.schedule import run_due_sync, run_schedule

logger = logging.getLogger("iam.worker")

SCHEDULE_SECONDS = 60
# The heartbeat is written at most this often.
HEARTBEAT_SECONDS = 10


class Command(BaseCommand):
    help = "Run provisioning jobs and the schedule."

    def add_arguments(self, parser):
        parser.add_argument("--once", action="store_true", help="Run one round and exit.")

    def handle(self, *args, once=False, **options):
        self.stopping = False
        self.last_beat = None
        signal.signal(signal.SIGTERM, self._stop)
        signal.signal(signal.SIGINT, self._stop)
        if not once:
            liveness.start_watchdog(settings.WORKER_STALL_SECONDS)
        last_schedule = float("-inf")
        logger.info("Worker started")
        while not self.stopping:
            close_old_connections()
            self.beat()
            due = once or time.monotonic() - last_schedule >= SCHEDULE_SECONDS
            # A failing schedule must not hold up the jobs, and the other way round.
            try:
                recover_stale_jobs()
                if due:
                    last_schedule = time.monotonic()
                    summary = run_schedule()
                    if any(summary.values()):
                        logger.info("Schedule: %s", summary)
            except Exception:  # noqa: BLE001 - the loop must survive database hiccups
                logger.exception("Worker schedule failed")
            try:
                ran = self.run_jobs()
            except Exception:  # noqa: BLE001 - the loop must survive database hiccups
                logger.exception("Worker round failed")
                ran = 0
            # Reads come after the jobs, one per minute at most: a slow connection
            # holds up the jobs for one read, and the jobs queued meanwhile run next.
            if due and not self.stopping:
                try:
                    run_due_sync()
                except Exception:  # noqa: BLE001 - the loop must survive database hiccups
                    logger.exception("Worker read failed")
            if once:
                break
            if not ran:
                self._sleep(settings.WORKER_POLL_SECONDS)
        logger.info("Worker stopped")

    def run_jobs(self, limit: int = 50) -> int:
        ran = 0
        while ran < limit and not self.stopping:
            job = claim_next_job()
            if job is None:
                break
            run_job(job)
            ran += 1
            self.beat()
        return ran

    def beat(self) -> None:
        """Tell the watchdog, the dashboard and /-/readyz that the worker is
        getting somewhere."""
        liveness.note_progress()
        now = time.monotonic()
        if self.last_beat is not None and now - self.last_beat < HEARTBEAT_SECONDS:
            return
        try:
            WorkerHeartbeat.objects.update_or_create(
                name="worker", defaults={"beat_at": timezone.now()}
            )
        except Exception as exc:  # noqa: BLE001 - the loop must survive database hiccups
            logger.warning("Worker heartbeat failed: %s", type(exc).__name__)
            return
        self.last_beat = now

    def _sleep(self, seconds: float) -> None:
        deadline = time.monotonic() + seconds
        while not self.stopping and time.monotonic() < deadline:
            liveness.note_progress()
            time.sleep(0.2)

    def _stop(self, *_):
        self.stopping = True

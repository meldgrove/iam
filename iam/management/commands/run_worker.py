"""The worker: runs queued jobs and, once a minute, the schedule.

One worker is enough for most organizations; more can run side by side,
because each job is claimed with a row lock. Stop it with SIGTERM: it
finishes the job it is running first."""

import logging
import signal
import time

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import close_old_connections
from django.utils import timezone

from iam.jobs import claim_next_job, recover_stale_jobs, run_job
from iam.models import WorkerHeartbeat
from iam.schedule import run_schedule

logger = logging.getLogger("iam.worker")

SCHEDULE_SECONDS = 60


class Command(BaseCommand):
    help = "Run provisioning jobs and the schedule."

    def add_arguments(self, parser):
        parser.add_argument("--once", action="store_true", help="Run one round and exit.")

    def handle(self, *args, once=False, **options):
        self.stopping = False
        signal.signal(signal.SIGTERM, self._stop)
        signal.signal(signal.SIGINT, self._stop)
        last_schedule = 0.0
        logger.info("Worker started")
        while not self.stopping:
            close_old_connections()
            try:
                recover_stale_jobs()
                if once or time.monotonic() - last_schedule >= SCHEDULE_SECONDS:
                    summary = run_schedule()
                    last_schedule = time.monotonic()
                    if any(summary.values()):
                        logger.info("Schedule: %s", summary)
                ran = self.run_jobs()
                WorkerHeartbeat.objects.update_or_create(
                    name="worker", defaults={"beat_at": timezone.now()}
                )
            except Exception:  # noqa: BLE001 - the loop must survive database hiccups
                logger.exception("Worker round failed")
                ran = 0
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
        return ran

    def _sleep(self, seconds: float) -> None:
        deadline = time.monotonic() + seconds
        while not self.stopping and time.monotonic() < deadline:
            time.sleep(0.2)

    def _stop(self, *_):
        self.stopping = True

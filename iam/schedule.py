"""What the worker starts on its own: onboarding before start dates,
offboarding after end dates, people imports and account comparisons."""

from __future__ import annotations

import logging
from contextlib import contextmanager
from datetime import datetime, timedelta

from django.db import connection as db_connection
from django.utils import timezone

from .adapters import AdapterKind
from .jobs import settle_finished_workflows
from .lifecycle import LifecycleError, start_workflow
from .models import AppSettings, Connection, Person, SyncRun, User, Workflow
from .sync import can_compare_accounts, compare_accounts, import_people, recover_stopped_reads

logger = logging.getLogger(__name__)

# PostgreSQL advisory locks: one worker at a time runs the schedule, and one
# at a time reads from connections. With several workers, one can run jobs
# while another reads a big directory.
SCHEDULE_LOCK_ID = 7_340_116_201
SYNC_LOCK_ID = 7_340_116_202
# A failed read comes again sooner than its interval: after 15 minutes, then
# 30, 60 and so on, never later than the interval.
SYNC_RETRY_MINUTES = 15


def today():
    return timezone.localdate()


def start_due_onboarding() -> list[Workflow]:
    settings = AppSettings.load()
    if not settings.automatic_onboarding:
        return []
    horizon = today() + timedelta(days=settings.onboarding_lead_days)
    started = []
    due = Person.objects.filter(
        status=Person.Status.PLANNED,
        start_date__isnull=False,
        start_date__lte=horizon,
        access_profile__isnull=False,
        onboarding_paused=False,
    ).exclude(end_date__lt=today())
    for person in due:
        try:
            started.append(
                start_workflow(
                    person,
                    Workflow.Kind.ONBOARDING,
                    automatic=True,
                    reason=f"starts on {person.start_date:%d.%m.%Y}",
                )
            )
        except LifecycleError as exc:
            logger.info("Onboarding of person %s not started: %s", person.pk, exc)
        except Exception:  # noqa: BLE001 - one person must not stop the schedule
            logger.exception("Onboarding of person %s failed to start", person.pk)
    return started


def start_due_offboarding() -> list[Workflow]:
    settings = AppSettings.load()
    if not settings.automatic_offboarding:
        return []
    started = []
    due = Person.objects.filter(
        status__in=[Person.Status.ONBOARDING, Person.Status.ACTIVE, Person.Status.PLANNED],
        end_date__isnull=False,
        end_date__lt=today(),
    )
    for person in due:
        if (
            person.status == Person.Status.PLANNED
            and not person.accounts.exists()
            and not User.objects.filter(person=person, is_active=True).exists()
        ):
            continue
        try:
            started.append(
                start_workflow(
                    person,
                    Workflow.Kind.OFFBOARDING,
                    automatic=True,
                    reason=f"last day was {person.end_date:%d.%m.%Y}",
                )
            )
        except LifecycleError as exc:
            logger.info("Offboarding of person %s not started: %s", person.pk, exc)
        except Exception:  # noqa: BLE001 - one person must not stop the schedule
            logger.exception("Offboarding of person %s failed to start", person.pk)
    return started


def _sync_kind(connection: Connection) -> str | None:
    """The read the schedule runs at the connection, if any."""
    if connection.kind == AdapterKind.HR_SOURCE:
        return SyncRun.Kind.PEOPLE
    if connection.kind == AdapterKind.PROVISIONING and can_compare_accounts(connection):
        return SyncRun.Kind.ACCOUNTS
    return None


def _sync_due_at(connection: Connection, kind: str) -> datetime | None:
    """When the connection's next read is due; None when it never ran."""
    if connection.last_sync_at is None:
        return None
    interval = connection.sync_interval_minutes
    # Enough runs for the pause after failures to reach the interval.
    history = max(interval // SYNC_RETRY_MINUTES, 1).bit_length() + 1
    failures = 0
    for status in (
        connection.sync_runs.filter(kind=kind)
        .order_by("-started_at", "-pk")[:history]
        .values_list("status", flat=True)
    ):
        if status != SyncRun.Status.FAILED:
            break
        failures += 1
    minutes = min(interval, SYNC_RETRY_MINUTES * 2 ** (failures - 1)) if failures else interval
    return connection.last_sync_at + timedelta(minutes=minutes)


def due_syncs() -> list[tuple[Connection, str]]:
    """The reads that are due, the most overdue first."""
    now = timezone.now()
    due = []
    for connection in Connection.objects.filter(enabled=True):
        if connection.sync_interval_minutes <= 0:
            continue
        kind = _sync_kind(connection)
        if kind is None:
            continue
        due_at = _sync_due_at(connection, kind) or now - timedelta(days=36500)
        if due_at <= now:
            due.append((due_at, connection.pk, connection, kind))
    return [(connection, kind) for *_, connection, kind in sorted(due, key=lambda d: d[:2])]


def run_due_sync() -> SyncRun | None:
    """Run the most overdue people import or account comparison. One read
    per call: a slow connection then holds up the worker's jobs for one
    read, not for all of them."""
    with exclusive(SYNC_LOCK_ID) as acquired:
        if not acquired:
            return None
        recover_stopped_reads()
        due = due_syncs()
        if not due:
            return None
        connection, kind = due[0]
        if kind == SyncRun.Kind.PEOPLE:
            return import_people(connection)
        return compare_accounts(connection)


@contextmanager
def exclusive(lock_id: int):
    """Yield whether this worker holds the lock now. With several workers,
    the first one to ask does the work and the others skip it, so nobody
    starts a workflow twice, counts removals or reads a connection at the
    same time."""
    if db_connection.vendor != "postgresql":
        yield True
        return
    with db_connection.cursor() as cursor:
        cursor.execute("SELECT pg_try_advisory_lock(%s)", [lock_id])
        acquired = cursor.fetchone()[0]
    try:
        yield acquired
    finally:
        if acquired:
            with db_connection.cursor() as cursor:
                cursor.execute("SELECT pg_advisory_unlock(%s)", [lock_id])


def run_schedule() -> dict:
    with exclusive(SCHEDULE_LOCK_ID) as acquired:
        if not acquired:
            return {}
        return {
            "onboarding": len(start_due_onboarding()),
            "offboarding": len(start_due_offboarding()),
            "settled": settle_finished_workflows(),
        }

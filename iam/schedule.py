"""What the worker starts on its own: onboarding before start dates,
offboarding after end dates, people imports and account comparisons."""

from __future__ import annotations

import logging
from datetime import timedelta

from django.utils import timezone

from .adapters import AdapterKind
from .lifecycle import LifecycleError, start_workflow
from .models import AppSettings, Connection, Person, Workflow
from .sync import can_compare_accounts, compare_accounts, import_people

logger = logging.getLogger(__name__)


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
        if person.status == Person.Status.PLANNED and not person.accounts.exists():
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
    return started


def run_due_syncs() -> int:
    now = timezone.now()
    count = 0
    for connection in Connection.objects.filter(enabled=True):
        interval = connection.sync_interval_minutes
        if interval <= 0:
            continue
        if connection.last_sync_at and connection.last_sync_at > now - timedelta(minutes=interval):
            continue
        kind = connection.kind
        if kind == AdapterKind.HR_SOURCE:
            import_people(connection)
            count += 1
        elif kind == AdapterKind.PROVISIONING and can_compare_accounts(connection):
            compare_accounts(connection)
            count += 1
    return count


def run_schedule() -> dict:
    return {
        "onboarding": len(start_due_onboarding()),
        "offboarding": len(start_due_offboarding()),
        "syncs": run_due_syncs(),
    }

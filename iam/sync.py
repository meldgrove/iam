"""Reads from connections: group import, account comparison and people import.

None of these change anything in a connected app. The account comparison
records what the app lists (iam.accounts links accounts to people) and
reports drift; fixing drift is a workflow an approver starts. The people import creates and updates people but never
offboards anyone by itself: an end date does that through the schedule.
"""

from __future__ import annotations

import logging
from datetime import date

from django.db import transaction
from django.utils import timezone

from . import audit
from .accounts import record_listing
from .adapters import AdapterKind
from .adapters.base import AdapterError, HRRecord
from .crypto import SecretUnreadable
from .models import AccessProfile, Account, Connection, Person, ProviderGroup, SyncRun, Team
from .policy import desired_access

logger = logging.getLogger(__name__)


class SyncError(Exception):
    pass


def _run(connection: Connection, kind: str, actor, body) -> SyncRun:
    run = SyncRun.objects.create(connection=connection, kind=kind, triggered_by=actor)
    try:
        adapter = connection.build_adapter()
        run.summary = body(adapter, run)
        run.status = SyncRun.Status.SUCCEEDED
    except (AdapterError, SecretUnreadable, SyncError) as exc:
        run.status = SyncRun.Status.FAILED
        run.error = str(exc)[:2000]
    except Exception as exc:  # noqa: BLE001 - a failed read is reported, not raised
        logger.exception("%s of connection %s failed", kind, connection.pk)
        run.status = SyncRun.Status.FAILED
        run.error = f"Unexpected error ({type(exc).__name__}). The details are in the log."
    run.finished_at = timezone.now()
    run.save()
    Connection.objects.filter(pk=connection.pk).update(last_sync_at=run.finished_at)
    audit.record(
        f"sync.{kind}",
        f"{run.get_kind_display()} from {connection.name}: {run.get_status_display().lower()}",
        actor=actor,
        target=run,
        details={"error": run.error} if run.error else {},
    )
    return run


def recover_stopped_reads() -> int:
    """Close scheduled reads whose worker stopped midway (a crash, the
    watchdog) as failed, and start their connection's pause after a failure
    now: a read that keeps stopping its worker then waits its turn instead
    of coming first again and holding up every other read. Only call it
    while holding the read lock (iam.schedule.run_due_sync), when no
    scheduled read runs."""
    now = timezone.now()
    stopped = SyncRun.objects.filter(status=SyncRun.Status.RUNNING, triggered_by=None)
    count = 0
    for run in stopped.select_related("connection"):
        with transaction.atomic():
            run.status = SyncRun.Status.FAILED
            run.error = "The worker stopped during this read."
            run.finished_at = now
            run.save(update_fields=["status", "error", "finished_at"])
            Connection.objects.filter(pk=run.connection_id).update(last_sync_at=now)
            audit.record(
                f"sync.{run.kind}",
                f"{run.get_kind_display()} from {run.connection.name}: failed",
                target=run,
                details={"error": run.error},
            )
        count += 1
    return count


# -- groups ------------------------------------------------------------------------


def import_groups(connection: Connection, actor=None) -> SyncRun:
    def body(adapter, run):
        if not adapter.supports_groups:
            raise SyncError(f"{connection.name} has no groups to import.")
        groups = adapter.list_groups()
        now = timezone.now()
        seen = set()
        created = updated = 0
        with transaction.atomic():
            for group in groups:
                seen.add(group.external_id)
                _, was_created = ProviderGroup.objects.update_or_create(
                    connection=connection,
                    external_id=group.external_id,
                    defaults={
                        "name": group.name[:255],
                        "path": group.path[:500],
                        "description": group.description,
                        "missing": False,
                        "manual": False,
                        "seen_at": now,
                    },
                )
                created += was_created
                updated += not was_created
            gone = (
                ProviderGroup.objects.filter(connection=connection, manual=False, missing=False)
                .exclude(external_id__in=seen)
                .update(missing=True)
            )
            Connection.objects.filter(pk=connection.pk).update(groups_synced_at=now)
        return {"groups": len(seen), "created": created, "updated": updated, "missing": gone}

    return _run(connection, SyncRun.Kind.GROUPS, actor, body)


# -- accounts ------------------------------------------------------------------------


def can_compare_accounts(connection: Connection) -> bool:
    cls = connection.adapter_class
    if cls is None or cls.kind != AdapterKind.PROVISIONING or not cls.supports_account_listing:
        return False
    try:
        adapter = connection.build_adapter()
    except Exception:  # noqa: BLE001
        return False
    reader = getattr(adapter, "can_read_accounts", None)
    return reader() if callable(reader) else True


# A scheduled comparison that would mark at least this many accounts, and
# more than half of those the app saw, as not found records nothing.
VANISHED_LIMIT = 10


def compare_accounts(connection: Connection, actor=None) -> SyncRun:
    def body(adapter, run):
        if not can_compare_accounts(connection):
            raise SyncError(f"{connection.name} cannot list accounts.")
        listed = list(adapter.list_accounts())
        if actor is None:
            _check_listing(connection, listed)
        return {**record_listing(connection, listed), **drift(connection)}

    return _run(connection, SyncRun.Kind.ACCOUNTS, actor, body)


def _check_listing(connection: Connection, listed) -> None:
    """Refuse a listing that lacks most of the accounts the app saw: such an
    answer (an empty page, a permission that changed) is more likely broken
    than true. A comparison an admin starts records it anyway."""
    seen = {account.external_id for account in listed}
    known = set(
        connection.accounts.exclude(external_id="")
        .exclude(status=Account.Status.MISSING)
        .values_list("external_id", flat=True)
    )
    vanished = len(known - seen)
    if vanished >= VANISHED_LIMIT and vanished * 2 > len(known):
        raise SyncError(
            f"{connection.name} listed {len(seen)} accounts, without {vanished} of the "
            f"{len(known)} it listed before. Nothing was recorded: an answer like this is more "
            "likely broken than true. If the accounts are really gone, use Compare accounts now."
        )


def drift(connection: Connection) -> dict:
    """Differences between desired access and the accounts last seen."""
    accounts = {a.person_id: a for a in connection.accounts.filter(person__isnull=False)}
    missing_access, unexpected_access = [], []
    people = Person.objects.exclude(status=Person.Status.ARCHIVED).select_related(
        "access_profile", "team"
    )
    for person in people:
        should_have = connection.pk in desired_access(person)
        account = accounts.get(person.pk)
        active = account is not None and account.status == Account.Status.ACTIVE
        if should_have and not active and person.status == Person.Status.ACTIVE:
            missing_access.append({"id": person.pk, "name": person.full_name})
        # Planned people keep what a cancelled onboarding created.
        if (
            active
            and not should_have
            and person.status
            in (
                Person.Status.PLANNED,
                Person.Status.OFFBOARDED,
                Person.Status.ACTIVE,
            )
        ):
            unexpected_access.append(
                {"id": person.pk, "name": person.full_name, "status": person.status}
            )
    unlinked = connection.accounts.filter(person__isnull=True, status=Account.Status.ACTIVE).count()
    return {
        "missing_access": missing_access,
        "unexpected_access": unexpected_access,
        "unlinked_active": unlinked,
    }


# -- people ----------------------------------------------------------------------------


def import_people(connection: Connection, actor=None) -> SyncRun:
    def body(adapter, run):
        if connection.kind != AdapterKind.HR_SOURCE:
            raise SyncError(f"{connection.name} does not import people.")
        records = list(adapter.list_people())
        summary = {
            "records": len(records),
            "created": [],
            "updated": [],
            "work_email_differs": [],
            "inactive_without_end_date": [],
            "not_listed": [],
        }
        listed_ids = set()
        for record in records:
            listed_ids.add(record.external_id)
            with transaction.atomic():
                _import_record(connection, record, summary, actor)
        not_listed = (
            Person.objects.filter(source_connection=connection)
            .exclude(source_id__in=listed_ids)
            .exclude(status__in=[Person.Status.OFFBOARDED, Person.Status.ARCHIVED])
        )
        summary["not_listed"] = [{"id": p.pk, "name": p.full_name} for p in not_listed[:100]]
        summary["skipped"] = [
            {"source_id": source_id, "reason": reason}
            for source_id, reason in getattr(adapter, "skipped", [])[:100]
        ]
        return summary

    return _run(connection, SyncRun.Kind.PEOPLE, actor, body)


IMPORTED_FIELDS = (
    "first_name",
    "last_name",
    "personal_email",
    "job_title",
    "person_type",
    "start_date",
    "end_date",
)


def _import_record(connection: Connection, record: HRRecord, summary: dict, actor) -> None:
    # Locked: the import saves the whole row, which must not write back a
    # status a workflow changed meanwhile.
    people = Person.objects.select_for_update(no_key=True)
    person = people.filter(source_connection=connection, source_id=record.external_id).first()
    email = (record.work_email or "").strip().lower()
    if person is None and email:
        person = people.filter(work_email__iexact=email, source_id="").first()
    values = {
        "first_name": record.first_name.strip()[:150],
        "last_name": record.last_name.strip()[:150],
        "personal_email": (record.personal_email or "").strip()[:254],
        "job_title": (record.job_title or "")[:200],
        "person_type": record.person_type
        if record.person_type in Person.Type.values
        else Person.Type.EMPLOYEE,
        "start_date": record.start_date,
        "end_date": record.end_date,
    }
    team = Team.objects.filter(name__iexact=record.team).first() if record.team else None
    manager = (
        Person.objects.filter(work_email__iexact=record.manager_email).first()
        if record.manager_email
        else None
    )
    if person is None:
        if not record.active or (record.end_date and record.end_date < date.today()):
            return
        if email and Person.objects.filter(work_email__iexact=email).exists():
            summary["work_email_differs"].append({"source_id": record.external_id, "email": email})
            return
        person = Person.objects.create(
            **values,
            work_email=email or None,
            team=team,
            manager=manager,
            access_profile=AccessProfile.default_for_type(values["person_type"]),
            source_connection=connection,
            source_id=record.external_id,
        )
        summary["created"].append({"id": person.pk, "name": person.full_name})
        audit.record(
            "person.imported",
            f"Imported {person.full_name} from {connection.name}",
            actor=actor,
            person=person,
            target=person,
        )
        return
    changed = {}
    for name in IMPORTED_FIELDS:
        if getattr(person, name) != values[name] and not (
            name == "personal_email" and not values[name]
        ):
            changed[name] = [str(getattr(person, name) or ""), str(values[name] or "")]
            setattr(person, name, values[name])
    if team is not None and person.team_id != team.pk:
        changed["team"] = [str(person.team or ""), team.name]
        person.team = team
    if manager is not None and manager.pk != person.pk and person.manager_id != manager.pk:
        changed["manager"] = [str(person.manager or ""), manager.full_name]
        person.manager = manager
    if not person.source_id:
        person.source_connection = connection
        person.source_id = record.external_id
        changed["source"] = ["", connection.name]
    if email and not person.work_email:
        person.work_email = email
        changed["work_email"] = ["", email]
    elif email and person.work_email and person.work_email.lower() != email:
        summary["work_email_differs"].append(
            {"id": person.pk, "name": person.full_name, "email": email}
        )
    if not record.active and not record.end_date:
        summary["inactive_without_end_date"].append({"id": person.pk, "name": person.full_name})
    if "start_date" in changed:
        person.onboarding_paused = False
    if changed:
        person.save()
        summary["updated"].append(
            {"id": person.pk, "name": person.full_name, "fields": sorted(changed)}
        )
        audit.record(
            "person.updated",
            f"Updated {person.full_name} from {connection.name}",
            actor=actor,
            person=person,
            target=person,
            details={"changes": changed},
        )

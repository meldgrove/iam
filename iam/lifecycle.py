"""Workflows: onboarding, access updates and offboarding of one person.

Starting a workflow captures the person's desired access and plans one job
per connection: make sure the account exists with the right groups, or
disable it. From there iam.jobs owns the jobs, their tasks and the
workflow's status: the worker runs the jobs, connections that cannot or may
not change accounts get a manual task instead, and the person's status
finishes when the workflow completes. This module plans jobs as queued, or
held for an admin's approval, and never changes the status of a job, a task
or a workflow after that.
"""

from __future__ import annotations

from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from . import audit
from .accounts import removable_groups
from .jobs import refresh_workflow, start_jobs, stop_workflow
from .models import ROLE_RANK, Account, AppSettings, Job, Person, User, Workflow
from .policy import (
    desired_access,
    entitled_access,
    managed_group_ids,
    provisioning_connections,
    snapshot,
)


class LifecycleError(Exception):
    """A workflow cannot start; the message explains why."""


# The person's statuses each workflow may start from. During onboarding, an
# access update restarts the onboarding (see start_workflow).
ALLOWED_FROM = {
    Workflow.Kind.ONBOARDING: {
        Person.Status.PLANNED,
        Person.Status.OFFBOARDED,
        Person.Status.ARCHIVED,
    },
    Workflow.Kind.ACCESS_UPDATE: {Person.Status.ONBOARDING, Person.Status.ACTIVE},
    Workflow.Kind.OFFBOARDING: {
        Person.Status.PLANNED,
        Person.Status.ONBOARDING,
        Person.Status.ACTIVE,
        Person.Status.OFFBOARDING,
        Person.Status.OFFBOARDED,
    },
}

STATUS_DURING = {
    Workflow.Kind.ONBOARDING: Person.Status.ONBOARDING,
    Workflow.Kind.OFFBOARDING: Person.Status.OFFBOARDING,
}

ARCHIVABLE = {Person.Status.PLANNED, Person.Status.OFFBOARDED}


def today():
    return timezone.localdate()


def can_start(person: Person, kind: str) -> bool:
    """Whether a workflow of this kind may start for the person now."""
    return person.status in ALLOWED_FROM[kind]


def can_archive(person: Person) -> bool:
    return person.status in ARCHIVABLE and not _has_open_workflow(person)


def _has_open_workflow(person: Person) -> bool:
    return person.workflows.filter(status__in=Workflow.OPEN_STATUSES).exists()


def start_workflow(
    person: Person,
    kind: str,
    *,
    actor: User | None = None,
    automatic: bool = False,
    reason: str = "",
) -> Workflow:
    with transaction.atomic():
        person = Person.objects.select_for_update(no_key=True).get(pk=person.pk)
        if not can_start(person, kind):
            raise LifecycleError(
                f"{Workflow.Kind(kind).label} cannot start while {person.full_name} is "
                f"{person.get_status_display().lower()}."
            )
        if kind == Workflow.Kind.ONBOARDING and person.end_date and person.end_date < today():
            # The schedule would offboard the person right after.
            raise LifecycleError(
                f"The last day of {person.full_name} ({person.end_date:%d.%m.%Y}) has passed. "
                "Clear or change it before onboarding."
            )
        if kind == Workflow.Kind.ACCESS_UPDATE and person.status == Person.Status.ONBOARDING:
            # Applying access during onboarding restarts the onboarding with
            # the new access, so the person still becomes active at its end.
            kind = Workflow.Kind.ONBOARDING
        if kind == Workflow.Kind.OFFBOARDING:
            if actor is not None:
                _check_may_offboard(person, actor)
            if automatic:
                # The last day may have changed since the schedule selected this person.
                if not person.end_date or person.end_date >= today():
                    raise LifecycleError("The person's last day has not passed.")
                reason = f"last day was {person.end_date:%d.%m.%Y}"
        if kind == Workflow.Kind.ONBOARDING and person.access_profile_id is None:
            raise LifecycleError("Choose an access profile before onboarding.")
        superseded = _supersede_open_workflows(person, actor)
        if kind in STATUS_DURING:
            person.status = STATUS_DURING[kind]
            person.onboarding_paused = False
            person.save(update_fields=["status", "onboarding_paused", "updated_at"])
        if kind == Workflow.Kind.OFFBOARDING:
            _deactivate_app_user(person, actor)
        workflow = Workflow.objects.create(
            person=person,
            kind=kind,
            desired=snapshot(person),
            automatic=automatic,
            reason=reason[:300],
            created_by=actor,
        )
        jobs = _plan_jobs(workflow, person)
        cancelled_tasks = start_jobs(workflow, jobs, actor=actor)
        audit.record(
            f"workflow.{kind}",
            f"Started {workflow.get_kind_display().lower()} of {person.full_name}"
            + (f" ({reason})" if reason else ""),
            actor=actor,
            person=person,
            target=workflow,
            details={
                "jobs": len(jobs),
                "automatic": automatic,
                "superseded_workflows": superseded,
                "cancelled_group_tasks": cancelled_tasks,
            },
        )
        # After the audit event: a workflow without jobs completes at once.
        refresh_workflow(workflow)
    return workflow


def _supersede_open_workflows(person: Person, actor) -> list[int]:
    ids = []
    for workflow in person.workflows.filter(status__in=Workflow.OPEN_STATUSES):
        stop_workflow(workflow, note="Replaced by a newer workflow.")
        ids.append(workflow.pk)
    return ids


def higher_role_app_user(person: Person, actor: User) -> User | None:
    user = User.objects.filter(person=person, is_active=True).first()
    if user is not None and ROLE_RANK.get(user.role, 0) > ROLE_RANK.get(actor.role, 0):
        return user
    return None


def _check_may_offboard(person: Person, actor: User) -> None:
    """Offboarding turns off the person's app user, so nobody may offboard
    someone with a higher role in this app than their own. The schedule
    (no actor) still offboards everyone after their last day."""
    if user := higher_role_app_user(person, actor):
        raise LifecycleError(
            f"{person.full_name} is {_with_article(user.get_role_display().lower())} of this "
            "app. Only someone with that role can start the offboarding."
        )


def _with_article(noun: str) -> str:
    return f"{'an' if noun[:1] in 'aeiou' else 'a'} {noun}"


def _deactivate_app_user(person: Person, actor) -> None:
    user = User.objects.filter(person=person, is_active=True).first()
    if user is None:
        return
    user.is_active = False
    user.save(update_fields=["is_active"])
    audit.record(
        "user.deactivated",
        f"Deactivated app user {user.email} because {person.full_name} is offboarded",
        actor=actor,
        person=person,
        target=user,
    )


def _plan_jobs(workflow: Workflow, person: Person) -> list[Job]:
    offboarding = workflow.kind == Workflow.Kind.OFFBOARDING
    desired = desired_access(person)
    # Offboarding never skips an app: turned-off connections get a manual task.
    entitled = entitled_access(person, include_disabled=True) if offboarding else {}
    accounts = {a.connection_id: a for a in person.accounts.all()}
    # A replaced job that is still running may yet create an account there.
    creating = set(
        person.jobs.filter(status=Job.Status.RUNNING, action=Job.Action.ENSURE_ACCOUNT).values_list(
            "connection_id", flat=True
        )
    )
    known = [Account.Status.ACTIVE, Account.Status.UNKNOWN]
    if offboarding:
        # A comparison can miss an account (a page that shifted, an answer
        # that only seemed empty); disabling one that is really gone changes
        # nothing.
        known.append(Account.Status.MISSING)
    settings = AppSettings.load()
    jobs = []
    for connection in provisioning_connections(include_disabled=offboarding):
        account = accounts.get(connection.pk)
        managed = sorted(removable_groups(connection.pk, person.pk, managed_group_ids(connection)))
        grant = desired.get(connection.pk)
        if grant is not None:
            job = Job(
                workflow=workflow,
                person=person,
                connection=connection,
                action=Job.Action.ENSURE_ACCOUNT,
                groups=grant.group_ids,
                managed_groups=managed,
            )
        else:
            has_account = account is not None and account.status in known
            if not has_account and connection.pk not in entitled and connection.pk not in creating:
                continue
            job = Job(
                workflow=workflow,
                person=person,
                connection=connection,
                action=Job.Action.DISABLE_ACCOUNT,
                managed_groups=managed,
            )
            if workflow.automatic and _over_removal_limit(connection, settings):
                job.status = Job.Status.HELD
                job.last_error = (
                    "More automatic removals than the daily limit allows; an admin must approve."
                )
        job.save()
        jobs.append(job)
    return jobs


def _over_removal_limit(connection, settings: AppSettings) -> bool:
    since = timezone.now() - timedelta(hours=24)
    recent = (
        Job.objects.filter(
            connection=connection,
            action=Job.Action.DISABLE_ACCOUNT,
            workflow__automatic=True,
            created_at__gte=since,
        )
        .exclude(status__in=[Job.Status.HELD, Job.Status.CANCELLED])
        .count()
    )
    return recent + 1 > settings.removal_limit_per_day


def cancel_workflow(workflow: Workflow, *, actor: User) -> list[Job]:
    """Cancel an onboarding or access update. Returns the jobs that were
    already running: the cancel comes too late for them, and their changes
    go ahead."""
    if workflow.kind == Workflow.Kind.OFFBOARDING:
        raise LifecycleError(
            "Offboarding cannot be cancelled. Retry or complete its steps instead."
        )
    with transaction.atomic():
        Person.objects.select_for_update(no_key=True).get(pk=workflow.person_id)
        workflow = Workflow.objects.select_for_update().get(pk=workflow.pk)
        if not workflow.is_open:
            raise LifecycleError("This workflow is already finished.")
        running = stop_workflow(workflow, actor=actor, note="Workflow cancelled.")
        person = workflow.person
        if workflow.kind == Workflow.Kind.ONBOARDING and person.status == Person.Status.ONBOARDING:
            # Cancelled stays cancelled: the schedule waits for a new start
            # date or access profile.
            person.status = Person.Status.PLANNED
            person.onboarding_paused = True
            person.save(update_fields=["status", "onboarding_paused", "updated_at"])
        summary = f"Cancelled {workflow.get_kind_display().lower()} of {person.full_name}"
        audit.record(
            "workflow.cancelled",
            f"{summary}. {too_late_for(running)}" if running else summary,
            actor=actor,
            person=person,
            target=workflow,
        )
    return running


def too_late_for(running: list[Job]) -> str:
    """What a cancel means for the jobs that were already running."""
    names = sorted({job.connection.name for job in running})
    where = ", ".join(names[:-1]) + f" and {names[-1]}" if len(names) > 1 else names[0]
    if len(running) == 1:
        return f"Too late for the job at {where}: it was already running, so its change goes ahead."
    return (
        f"Too late for the jobs at {where}: they were already running, so their changes go ahead."
    )


@transaction.atomic
def archive_person(person: Person, *, actor: User) -> None:
    # Serialize with workflow transitions before checking eligibility.
    person.refresh_from_db(from_queryset=Person.objects.select_for_update(no_key=True))
    if person.status not in ARCHIVABLE:
        raise LifecycleError("Only planned or offboarded people can be archived.")
    if _has_open_workflow(person):
        raise LifecycleError("Finish or cancel the open workflow first.")
    person.status = Person.Status.ARCHIVED
    person.save(update_fields=["status", "updated_at"])
    audit.record(
        "person.archived", f"Archived {person.full_name}", actor=actor, person=person, target=person
    )

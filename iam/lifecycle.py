"""Workflows: onboarding, access updates and offboarding of one person.

Starting a workflow captures the person's desired access and plans one job
per connection: make sure the account exists with the right groups, or
disable it. The worker runs the jobs (iam.jobs); connections that cannot or
may not change accounts get a manual task instead.
"""

from __future__ import annotations

from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from . import audit
from .models import Account, AppSettings, Job, Person, Task, User, Workflow
from .policy import (
    desired_access,
    entitled_access,
    managed_group_ids,
    provisioning_connections,
    snapshot,
)


class LifecycleError(Exception):
    """A workflow cannot start; the message explains why."""


ALLOWED_FROM = {
    Workflow.Kind.ONBOARDING: {
        Person.Status.PLANNED,
        Person.Status.ONBOARDING,
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


def start_workflow(
    person: Person,
    kind: str,
    *,
    actor: User | None = None,
    automatic: bool = False,
    reason: str = "",
) -> Workflow:
    from .jobs import hand_to_person, notify_after_commit

    with transaction.atomic():
        person = Person.objects.select_for_update().get(pk=person.pk)
        if person.status not in ALLOWED_FROM[kind]:
            raise LifecycleError(
                f"{Workflow.Kind(kind).label} cannot start while {person.full_name} is "
                f"{person.get_status_display().lower()}."
            )
        if kind == Workflow.Kind.ONBOARDING and person.access_profile_id is None:
            raise LifecycleError("Choose an access profile before onboarding.")
        superseded = _supersede_open_workflows(person, actor)
        if kind in STATUS_DURING:
            person.status = STATUS_DURING[kind]
            person.save(update_fields=["status", "updated_at"])
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
            },
        )
        tasks = []
        for job in jobs:
            if job.status == Job.Status.QUEUED and not job.connection.writes_automatically:
                tasks.append(hand_to_person(job))
        refresh_workflow(workflow)
        notify_after_commit([task for task in tasks if task is not None])
    return workflow


def _supersede_open_workflows(person: Person, actor) -> list[int]:
    ids = []
    for workflow in person.workflows.filter(
        status__in=[Workflow.Status.RUNNING, Workflow.Status.WAITING, Workflow.Status.FAILED]
    ):
        for job in workflow.jobs.exclude(
            status__in=[Job.Status.SUCCEEDED, Job.Status.CANCELLED, Job.Status.RUNNING]
        ):
            cancel_job(job, note="Replaced by a newer workflow.")
        workflow.status = Workflow.Status.CANCELLED
        workflow.finished_at = timezone.now()
        workflow.save(update_fields=["status", "finished_at"])
        ids.append(workflow.pk)
    # Groups an earlier job left unfinished: the new workflow plans them again.
    Task.objects.filter(
        person=person,
        kind=Task.Kind.FAILURE,
        status=Task.Status.OPEN,
        job__status=Job.Status.SUCCEEDED,
    ).update(
        status=Task.Status.CANCELLED,
        completed_at=timezone.now(),
        completed_by=actor,
        note="Replaced by a newer workflow.",
    )
    return ids


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
    settings = AppSettings.load()
    jobs = []
    for connection in provisioning_connections(include_disabled=offboarding):
        account = accounts.get(connection.pk)
        applied = set(account.applied_groups or []) if account else set()
        managed = sorted(managed_group_ids(connection) | applied)
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
            has_account = account is not None and account.status in (
                Account.Status.ACTIVE,
                Account.Status.UNKNOWN,
            )
            if not has_account and connection.pk not in entitled:
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


def refresh_workflow(workflow: Workflow) -> Workflow:
    """Derive the workflow's status from its jobs and finish the person's
    status change when everything is done."""
    if workflow.status == Workflow.Status.CANCELLED:
        return workflow
    statuses = set(workflow.jobs.values_list("status", flat=True))
    if statuses <= {Job.Status.SUCCEEDED, Job.Status.CANCELLED}:
        status = Workflow.Status.COMPLETED
    elif Job.Status.FAILED in statuses:
        status = Workflow.Status.FAILED
    elif statuses & {Job.Status.QUEUED, Job.Status.RUNNING}:
        status = Workflow.Status.RUNNING
    else:
        status = Workflow.Status.WAITING
    if status == workflow.status:
        return workflow
    workflow.status = status
    workflow.finished_at = timezone.now() if status == Workflow.Status.COMPLETED else None
    workflow.save(update_fields=["status", "finished_at"])
    if status == Workflow.Status.COMPLETED:
        _finish_person_status(workflow)
    return workflow


def _finish_person_status(workflow: Workflow) -> None:
    person = Person.objects.select_for_update().get(pk=workflow.person_id)
    latest = (
        person.workflows.exclude(status=Workflow.Status.CANCELLED)
        .order_by("-created_at", "-pk")
        .first()
    )
    if latest is None or latest.pk != workflow.pk:
        return
    if workflow.kind == Workflow.Kind.ONBOARDING and person.status == Person.Status.ONBOARDING:
        person.status = Person.Status.ACTIVE
    elif workflow.kind == Workflow.Kind.OFFBOARDING and person.status == Person.Status.OFFBOARDING:
        person.status = Person.Status.OFFBOARDED
    else:
        return
    person.save(update_fields=["status", "updated_at"])
    audit.record(
        "person.status",
        f"{person.full_name} is now {person.get_status_display().lower()}",
        person=person,
        target=workflow,
    )


def cancel_job(job: Job, *, actor=None, note: str = "") -> None:
    job.status = Job.Status.CANCELLED
    job.finished_at = timezone.now()
    job.locked_until = None
    job.save(update_fields=["status", "finished_at", "locked_until"])
    job.tasks.filter(status=Task.Status.OPEN).update(
        status=Task.Status.CANCELLED,
        completed_at=timezone.now(),
        completed_by=actor,
        note=note,
    )


def cancel_workflow(workflow: Workflow, *, actor: User) -> None:
    if workflow.kind == Workflow.Kind.OFFBOARDING:
        raise LifecycleError(
            "Offboarding cannot be cancelled. Retry or complete its steps instead."
        )
    with transaction.atomic():
        workflow = Workflow.objects.select_for_update().get(pk=workflow.pk)
        if not workflow.is_open:
            raise LifecycleError("This workflow is already finished.")
        for job in workflow.jobs.exclude(status__in=[Job.Status.SUCCEEDED, Job.Status.CANCELLED]):
            cancel_job(job, actor=actor, note="Workflow cancelled.")
        workflow.status = Workflow.Status.CANCELLED
        workflow.finished_at = timezone.now()
        workflow.save(update_fields=["status", "finished_at"])
        person = workflow.person
        if workflow.kind == Workflow.Kind.ONBOARDING and person.status == Person.Status.ONBOARDING:
            person.status = Person.Status.PLANNED
            person.save(update_fields=["status", "updated_at"])
        audit.record(
            "workflow.cancelled",
            f"Cancelled {workflow.get_kind_display().lower()} of {person.full_name}",
            actor=actor,
            person=person,
            target=workflow,
        )


def archive_person(person: Person, *, actor: User) -> None:
    if person.status not in (Person.Status.OFFBOARDED, Person.Status.PLANNED):
        raise LifecycleError("Only planned or offboarded people can be archived.")
    if person.workflows.filter(
        status__in=[Workflow.Status.RUNNING, Workflow.Status.WAITING, Workflow.Status.FAILED]
    ).exists():
        raise LifecycleError("Finish or cancel the open workflow first.")
    person.status = Person.Status.ARCHIVED
    person.save(update_fields=["status", "updated_at"])
    audit.record(
        "person.archived", f"Archived {person.full_name}", actor=actor, person=person, target=person
    )

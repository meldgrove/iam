"""Jobs and their tasks: one account change at one connection.

This is the only module that changes the status of a job, a task or a
workflow. Lifecycle plans a workflow's jobs and hands them over with
start_jobs, or stops them with stop_workflow; the worker claims and runs
them; people retry, approve, reject, complete or cancel them. Every change
ends the same way: the workflow's status is worked out again from its jobs
(refresh_workflow), which also finishes the person's status, and people
hear about new tasks once the change commits.

The worker claims queued jobs one at a time. A connection that may change
accounts gets the adapter call; any other connection gets a manual task. A
failure that clears on its own (timeouts, rate limits, server errors) runs
again with growing pauses; any other failure, or the last retry, hands the
job to a person as a failure task. Groups never hold up an account: once
the account is created, reactivated or disabled, a group step that keeps
failing becomes a task while the job succeeds. Nothing here stores
passwords: a new account's initial password is emailed once to the personal
address and forgotten.
"""

from __future__ import annotations

import logging
import uuid
from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.db.models import Exists, OuterRef
from django.utils import timezone

from . import accounts, audit
from .adapters import AdapterKind
from .adapters.base import (
    AccountSpec,
    AdapterError,
    DisableResult,
    EnsureResult,
)
from .crypto import SecretUnreadable
from .models import (
    Account,
    AppSettings,
    Connection,
    Job,
    Person,
    ProviderGroup,
    Task,
    User,
    Workflow,
)

logger = logging.getLogger(__name__)

MAX_ATTEMPTS = 6


def backoff(attempts: int) -> timedelta:
    return timedelta(minutes=min(2 ** max(attempts - 1, 0), 60))


def account_spec(person: Person) -> AccountSpec:
    return AccountSpec(
        first_name=person.first_name,
        last_name=person.last_name,
        username=person.login_name,
        email=person.work_email or "",
        personal_email=person.personal_email,
        job_title=person.job_title,
        person_type=person.person_type,
        team=person.team.name if person.team_id else "",
        manager_email=(person.manager.work_email or "") if person.manager_id else "",
        start_date=person.start_date,
        end_date=person.end_date,
    )


def managed_groups(job: Job) -> set[str]:
    """The groups the job may remove: the policy's managed groups when it was
    planned, plus the groups the app has put the account in since."""
    return accounts.removable_groups(job.connection_id, job.person_id, job.managed_groups)


def group_labels(job: Job) -> list[str]:
    labels = dict(
        ProviderGroup.objects.filter(
            connection=job.connection, external_id__in=job.groups
        ).values_list("external_id", "name")
    )
    return [labels.get(gid, gid) for gid in job.groups]


# -- starting and stopping -----------------------------------------------------


def start_jobs(workflow: Workflow, jobs: list[Job], *, actor: User | None = None) -> list[int]:
    """Take over the jobs a new workflow planned: cancel the group tasks they
    replace, and hand jobs at connections without automation to a person.
    Returns the cancelled group tasks. The caller refreshes the workflow once
    it has recorded the start."""
    cancelled = _cancel_replaced_group_tasks(workflow.person_id, jobs, actor)
    tasks = [
        _hand_to_person(job)
        for job in jobs
        if job.status == Job.Status.QUEUED and not job.connection.writes_automatically
    ]
    notify_after_commit(tasks)
    return cancelled


def _cancel_replaced_group_tasks(person_id: int, jobs: list[Job], actor) -> list[int]:
    """Groups an earlier job left unfinished, at the connections the new
    workflow plans again. Elsewhere the task stays open: nothing else will
    finish those groups."""
    tasks = Task.objects.filter(
        person_id=person_id,
        kind=Task.Kind.FAILURE,
        status=Task.Status.OPEN,
        job__status=Job.Status.SUCCEEDED,
        job__connection__in=[job.connection_id for job in jobs],
    )
    ids = list(tasks.values_list("pk", flat=True))
    _close_tasks(
        Task.objects.filter(pk__in=ids),
        Task.Status.CANCELLED,
        actor=actor,
        note="Replaced by a newer workflow.",
    )
    return ids


def stop_workflow(workflow: Workflow, *, actor: User | None = None, note: str) -> None:
    """Cancel the workflow and its unfinished jobs. A running job finishes:
    its change may already have happened in the connected app. The lock
    keeps the worker from claiming a job meanwhile; it covers only the job
    rows, not the connections the ordering joins."""
    unfinished = workflow.jobs.select_for_update(of=("self",)).exclude(
        status__in=[Job.Status.SUCCEEDED, Job.Status.CANCELLED, Job.Status.RUNNING]
    )
    for job in unfinished:
        cancel_job(job, actor=actor, note=note)
    workflow.status = Workflow.Status.CANCELLED
    workflow.finished_at = timezone.now()
    workflow.save(update_fields=["status", "finished_at"])


def cancel_job(job: Job, *, actor: User | None = None, note: str = "") -> None:
    job.status = Job.Status.CANCELLED
    job.finished_at = timezone.now()
    job.locked_until = None
    job.save(update_fields=["status", "finished_at", "locked_until"])
    _close_tasks(_work_tasks(job), Task.Status.CANCELLED, actor=actor, note=note)


def _work_tasks(job: Job):
    """The job's open tasks that stand for its change. Sign-in details for
    an account the job created must still reach the person, whatever
    happens to the job afterwards: nothing creates them again."""
    return job.tasks.filter(status=Task.Status.OPEN).exclude(kind=Task.Kind.CREDENTIALS)


def refresh_workflow(workflow: Workflow) -> Workflow:
    """Derive the workflow's status from its jobs and finish the person's
    status change when everything is done. The locks make changes to its
    jobs take turns here: two jobs that finish at once would each see the
    other still running, and the workflow would never complete. The person
    comes first, as when a workflow starts or is cancelled."""
    Person.objects.select_for_update().get(pk=workflow.person_id)
    locked = Workflow.objects.select_for_update().get(pk=workflow.pk)
    workflow.status, workflow.finished_at = locked.status, locked.finished_at
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


def settle_finished_workflows() -> int:
    """Complete open workflows whose jobs are all done. Every job change
    already does this; it catches workflows a crash or an older version of
    this app left open."""
    open_jobs = Job.objects.filter(workflow=OuterRef("pk"), status__in=Job.OPEN_STATUSES)
    stuck = Workflow.objects.filter(status__in=Workflow.OPEN_STATUSES).exclude(Exists(open_jobs))
    count = 0
    for pk in stuck.values_list("pk", flat=True):
        with transaction.atomic():
            workflow = refresh_workflow(Workflow.objects.get(pk=pk))
            count += workflow.status == Workflow.Status.COMPLETED
    return count


def _settle(job: Job, *new_tasks: Task | None) -> None:
    """End every change to a job: work out its workflow's status again, and
    tell people about new tasks once the change commits."""
    refresh_workflow(job.workflow)
    notify_after_commit(list(new_tasks))


def _replaced(job: Job) -> bool:
    """Whether a newer workflow replaced the job's (or it was cancelled)."""
    return job.workflow.status == Workflow.Status.CANCELLED


def _claimed_elsewhere(job: Job, claim: Job) -> bool:
    """Whether another worker claimed the job after this claim's lease ran
    out. That worker's claim, and nothing this one does, settles the job."""
    return job.claim != claim.claim


def _locked(job: Job) -> Job:
    """The job, locked for a change. The person comes first, as when a
    workflow starts, is cancelled or works out its status: with one order
    everywhere, two changes never wait for each other."""
    Person.objects.select_for_update().get(pk=job.person_id)
    return Job.objects.select_for_update().get(pk=job.pk)


def _close_tasks(tasks, status: str, *, actor: User | None = None, note: str = "") -> None:
    tasks.update(status=status, completed_at=timezone.now(), completed_by=actor, note=note[:2000])


# -- claiming --------------------------------------------------------------


def claim_next_job() -> Job | None:
    now = timezone.now()
    # A job waits while another job for the same person and app runs, so an
    # older change can never land after a newer one.
    busy = Job.objects.filter(
        status=Job.Status.RUNNING,
        person_id=OuterRef("person_id"),
        connection_id=OuterRef("connection_id"),
    )
    with transaction.atomic():
        job = (
            # Locks only the job row, not the workflow the filter joins.
            Job.objects.select_for_update(skip_locked=True, of=("self",))
            .filter(status=Job.Status.QUEUED, next_attempt_at__lte=now)
            .exclude(workflow__status=Workflow.Status.CANCELLED)
            .exclude(Exists(busy))
            .order_by("next_attempt_at", "pk")
            .first()
        )
        if job is None:
            return None
        job.status = Job.Status.RUNNING
        job.attempts += 1
        job.locked_until = now + timedelta(seconds=settings.JOB_LEASE_SECONDS)
        job.claim = uuid.uuid4()
        job.save(update_fields=["status", "attempts", "locked_until", "claim"])
    return job


def recover_stale_jobs() -> int:
    """Jobs whose worker stopped mid-run go back to the queue. The adapter
    operations are idempotent, so running one again is safe."""
    count = 0
    stale = Job.objects.filter(status=Job.Status.RUNNING, locked_until__lt=timezone.now())
    for job in stale:
        with transaction.atomic():
            job = _locked(job)
            if job.status != Job.Status.RUNNING:
                continue
            if _replaced(job):
                cancel_job(job, note="The worker stopped; the workflow was replaced meanwhile.")
                _settle(job)
                count += 1
                continue
            job.status = Job.Status.QUEUED
            job.locked_until = None
            job.next_attempt_at = timezone.now()
            job.last_error = "The worker stopped while running this job; it runs again."
            job.save(update_fields=["status", "locked_until", "next_attempt_at", "last_error"])
            count += 1
    return count


# -- running ----------------------------------------------------------------


def run_job(job: Job) -> Job:
    connection = Connection.objects.get(pk=job.connection_id)
    person = Person.objects.select_related("team", "manager").get(pk=job.person_id)
    if not connection.writes_automatically or not _can_write(connection):
        with transaction.atomic():
            claim, job = job, _locked(job)
            if job.status not in (Job.Status.RUNNING, Job.Status.QUEUED):
                return job
            if _claimed_elsewhere(job, claim):
                return job
            if _replaced(job):
                cancel_job(job, note="Replaced by a newer workflow.")
                _settle(job)
            else:
                _settle(job, _hand_to_person(job))
        return job
    spec = account_spec(person)
    account = Account.objects.filter(connection=connection, person=person).first()
    external_id = account.external_id if account else ""
    try:
        adapter = connection.build_adapter()
        # Check whose account the app is about to change before changing it.
        found = adapter.find_account(spec, external_id)
        if found is not None and found.external_id:
            owner = accounts.other_owner(connection, found.external_id, person)
            if owner is not None:
                return fail_job(
                    job,
                    f"The {connection.name} account {found.username or found.external_id} "
                    f"belongs to {owner}. Nothing was changed. Check the work email "
                    "and username.",
                    retryable=False,
                )
            external_id = found.external_id
        # Hand over the account whose owner was checked, so the change cannot
        # land on another account a second lookup finds.
        if job.action == Job.Action.ENSURE_ACCOUNT:
            result = adapter.ensure_account(
                spec,
                external_id=external_id,
                groups=set(job.groups),
                managed_groups=managed_groups(job),
                found=found,
            )
        else:
            result = adapter.disable_account(
                spec, external_id=external_id, managed_groups=managed_groups(job), found=found
            )
    except AdapterError as exc:
        return fail_job(job, str(exc), retryable=exc.retryable)
    except SecretUnreadable as exc:
        return fail_job(job, str(exc), retryable=False)
    except Exception as exc:  # noqa: BLE001 - every failure must reach a person
        logger.exception("Job %s failed unexpectedly", job.pk)
        _report_to_sentry(exc)
        return fail_job(
            job,
            f"Unexpected error ({type(exc).__name__}). The details are in the worker log.",
            retryable=False,
        )
    error = result.group_error
    if error is not None and not isinstance(error, AdapterError):
        logger.error("Job %s: the group step failed unexpectedly", job.pk, exc_info=error)
        _report_to_sentry(error)
    elif error is not None and error.retryable and job.attempts < MAX_ATTEMPTS:
        # The account step is done; the job runs again for the groups. A new
        # account's password must reach the person now: the next run finds
        # the account and generates none.
        conflict = record_account_step(job, result)
        if conflict:
            return fail_job(job, conflict, retryable=False)
        hand_over_sign_in(job, result)
        return fail_job(job, str(error), retryable=True)
    return succeed_job(job, result)


def record_account_step(job: Job, result: EnsureResult | DisableResult) -> str:
    """Audit an account that changed while its groups did not, before the
    job runs again. Returns why the account cannot be linked, if so."""
    with transaction.atomic():
        job = _locked(job)
        if isinstance(result, EnsureResult):
            # IAM must know a new account even if a newer workflow replaces
            # this job. A disabled account stays active in IAM until its
            # groups are done, so a newer workflow still plans a job there.
            try:
                with transaction.atomic():
                    accounts.record_job_result(job, result)
            except accounts.AccountConflict as exc:
                return str(exc)
        audit.record(
            f"job.{job.action}",
            f"{_success_summary(job, result)}. The groups are not done yet.",
            person=job.person,
            target=job,
            details=_result_summary(result),
        )
    return ""


def _group_error_message(error: Exception) -> str:
    if isinstance(error, AdapterError):
        return str(error)
    return f"Unexpected error ({type(error).__name__}). The details are in the worker log."


def _can_write(connection: Connection) -> bool:
    cls = connection.adapter_class
    return bool(cls and cls.kind == AdapterKind.PROVISIONING and cls.supports_write)


def _report_to_sentry(exc: Exception) -> None:
    if settings.SENTRY_DSN:
        import sentry_sdk

        sentry_sdk.capture_exception(exc)


def succeed_job(job: Job, result: EnsureResult | DisableResult) -> Job:
    conflict = ""
    group_task = None
    claim = job
    with transaction.atomic():
        job = _locked(job)
        # The change happened in the connected app, so the account and the
        # initial password count even when the job was cancelled, or handed
        # back to the queue after its lease ran out, while it ran. A worker
        # that claimed it again since then settles the job.
        try:
            with transaction.atomic():
                accounts.record_job_result(job, result)
        except accounts.AccountConflict as exc:
            conflict = str(exc)
        current = not conflict and not _claimed_elsewhere(job, claim)
        if current:
            job.status = Job.Status.SUCCEEDED
            job.finished_at = timezone.now()
            job.locked_until = None
            job.last_error = ""
            job.result = _result_summary(result)
            job.save(
                update_fields=["status", "finished_at", "locked_until", "last_error", "result"]
            )
            _close_tasks(
                job.tasks.filter(status=Task.Status.OPEN, kind=Task.Kind.FAILURE),
                Task.Status.DONE,
                note="Resolved by a retry.",
            )
            if result.group_error is not None and _workflow_is_current(job):
                group_task = _open_group_task(job, result.group_error)
        if not conflict:
            audit.record(
                f"job.{job.action}",
                _success_summary(job, result),
                person=job.person,
                target=job,
                details=_result_summary(result),
            )
        if current:
            _settle(job, group_task)
    if conflict:
        return fail_job(claim, conflict, retryable=False)
    hand_over_sign_in(job, result)
    return job


def _result_summary(result: EnsureResult | DisableResult) -> dict:
    summary = {
        "groups_removed": list(result.groups_removed),
        "notes": list(result.notes),
    }
    if result.group_error is not None:
        summary["group_error"] = _group_error_message(result.group_error)[:500]
    if isinstance(result, EnsureResult):
        summary.update(
            created=result.created,
            reactivated=result.reactivated,
            groups_added=list(result.groups_added),
            password_generated=bool(result.initial_password),
        )
    else:
        summary.update(changed=result.changed, found=result.account is not None)
    if result.account is not None:
        summary["account"] = (
            result.account.username or result.account.email or result.account.external_id
        )
    return summary


def _success_summary(job: Job, result) -> str:
    name = f"{job.connection.name} account of {job.person.full_name}"
    if isinstance(result, EnsureResult):
        verb = "Created" if result.created else "Reactivated" if result.reactivated else "Checked"
        return f"{verb} the {name}"
    if result.account is None:
        return f"No {name} to disable"
    return f"Disabled the {name}" if result.changed else f"The {name} was already disabled"


def fail_job(job: Job, message: str, *, retryable: bool) -> Job:
    task = None
    claim = job
    with transaction.atomic():
        job = _locked(job)
        if job.status not in (Job.Status.RUNNING, Job.Status.QUEUED):
            return job
        if _claimed_elsewhere(job, claim):
            return job
        if _replaced(job):
            # A newer workflow replaced this one while the job ran.
            cancel_job(job, note=f"Replaced by a newer workflow after: {message[:500]}")
            _settle(job)
            return job
        job.last_error = message[:2000]
        job.locked_until = None
        if retryable and job.attempts < MAX_ATTEMPTS:
            job.status = Job.Status.QUEUED
            job.next_attempt_at = timezone.now() + backoff(job.attempts)
            job.save(update_fields=["status", "last_error", "locked_until", "next_attempt_at"])
            return job
        job.status = Job.Status.FAILED
        job.save(update_fields=["status", "last_error", "locked_until"])
        task = _open_task(
            job,
            Task.Kind.FAILURE,
            f"Fix: {job.get_action_display().lower()} at {job.connection.name} for {job.person.full_name}",
            "\n\n".join(
                [
                    f"The app stopped after {job.attempts} attempt(s): {message}",
                    "Fix the cause and press Retry on the job, or make the change by hand "
                    "and complete this task.",
                    _manual_steps(job),
                ]
            ),
        )
        audit.record(
            "job.failed",
            f"{job.get_action_display()} at {job.connection.name} for {job.person.full_name} failed",
            person=job.person,
            target=job,
            details={"error": message[:500], "attempts": job.attempts},
        )
        _settle(job, task)
    return job


def _workflow_is_current(job: Job) -> bool:
    """Whether no newer workflow replaced the job's. Takes the person lock
    that starting a workflow takes: a newer one either replaced this one
    already, or starts after this commits and cancels its group task if it
    plans a job at the same connection."""
    Person.objects.select_for_update().get(pk=job.person_id)
    return (
        Workflow.objects.filter(pk=job.workflow_id)
        .exclude(status=Workflow.Status.CANCELLED)
        .exists()
    )


def _open_group_task(job: Job, error: Exception) -> Task | None:
    """The account step is done; hand the unfinished groups to a person."""
    adapter = job.connection.adapter_class
    label = adapter.group_label if adapter else "Groups"
    disabling = job.action == Job.Action.DISABLE_ACCOUNT
    done = "is disabled" if disabling else "is ready"
    steps = []
    if not disabling and job.groups:
        steps.append(f"Make sure they are in: {', '.join(sorted(group_labels(job)))}.")
    remove = _group_names(job, managed_groups(job) - set(job.groups))
    if remove:
        steps.append(f"Remove them from these, if a member: {remove}.")
    return _open_task(
        job,
        Task.Kind.FAILURE,
        f"Fix: {label} of {job.person.full_name} at {job.connection.name}",
        "\n\n".join(
            [
                f"The {job.connection.name} account {done}, but the app could not update "
                f"its {label}: {_group_error_message(error)}",
                "Fix the cause, make these changes by hand, then complete this task.",
                "\n".join(steps),
            ]
        ),
    )


def _group_names(job: Job, ids: set[str]) -> str:
    labels = dict(
        ProviderGroup.objects.filter(connection=job.connection, external_id__in=ids).values_list(
            "external_id", "name"
        )
    )
    return ", ".join(sorted(labels.get(gid, gid) for gid in ids))


def _manual_steps(job: Job) -> str:
    try:
        adapter = job.connection.build_adapter()
        steps = adapter.manual_instructions(job.action, account_spec(job.person), group_labels(job))
    except Exception:  # noqa: BLE001 - instructions are best effort
        verb = "Disable" if job.action == Job.Action.DISABLE_ACCOUNT else "Create or update"
        steps = f"{verb} the {job.connection.name} account of {job.person.full_name}."
    remove = managed_groups(job) - set(job.groups)
    if job.action == Job.Action.ENSURE_ACCOUNT and remove:
        steps += f"\nRemove from these, if a member: {_group_names(job, remove)}."
    return steps


def _open_task(job: Job, kind: str, title: str, instructions: str) -> Task | None:
    existing = job.tasks.filter(status=Task.Status.OPEN, kind=kind).first()
    if existing is not None:
        existing.instructions = instructions
        existing.save(update_fields=["instructions"])
        return None
    return Task.objects.create(
        kind=kind,
        title=title[:300],
        instructions=instructions,
        person=job.person,
        job=job,
        connection=job.connection,
    )


def _hand_to_person(job: Job) -> Task | None:
    """Turn a job at a connection without automation into a manual task."""
    job.status = Job.Status.MANUAL
    job.locked_until = None
    job.save(update_fields=["status", "locked_until"])
    verb = "Remove" if job.action == Job.Action.DISABLE_ACCOUNT else "Give"
    preposition = "from" if job.action == Job.Action.DISABLE_ACCOUNT else "access to"
    title = f"{verb} {job.person.full_name} {preposition} {job.connection.name}"
    reason = ""
    if not job.connection.enabled:
        reason = "The connection is turned off, so the app makes no changes there."
    elif job.connection.mode == Connection.Mode.READ_ONLY:
        reason = "The connection is read only, so the app makes no changes there."
    instructions = "\n\n".join(filter(None, [reason, _manual_steps(job)]))
    return _open_task(job, Task.Kind.CHANGE, title, instructions)


# -- operator actions ----------------------------------------------------------


class ActionError(Exception):
    pass


def retry_job(job: Job, *, actor: User) -> None:
    with transaction.atomic():
        job = _locked(job)
        if job.status not in (Job.Status.FAILED, Job.Status.MANUAL):
            raise ActionError("Only failed jobs and jobs with a manual task can run again.")
        if _replaced(job):
            raise ActionError("The workflow was replaced or cancelled; start a new one instead.")
        job.status = Job.Status.QUEUED
        job.attempts = 0
        job.next_attempt_at = timezone.now()
        job.save(update_fields=["status", "attempts", "next_attempt_at"])
        _close_tasks(
            _work_tasks(job), Task.Status.CANCELLED, actor=actor, note="The job runs again."
        )
        audit.record("job.retry", f"Retried: {job}", actor=actor, person=job.person, target=job)
        _settle(job)


def approve_job(job: Job, *, actor: User) -> None:
    with transaction.atomic():
        job = _locked(job)
        if job.status != Job.Status.HELD:
            raise ActionError("This job is not waiting for approval.")
        job.status = Job.Status.QUEUED
        job.next_attempt_at = timezone.now()
        job.last_error = ""
        job.save(update_fields=["status", "next_attempt_at", "last_error"])
        audit.record("job.approved", f"Approved: {job}", actor=actor, person=job.person, target=job)
        _settle(job)


def reject_job(job: Job, *, actor: User) -> None:
    with transaction.atomic():
        job = _locked(job)
        if job.status != Job.Status.HELD:
            raise ActionError("This job is not waiting for approval.")
        cancel_job(job, actor=actor, note="Rejected by an admin.")
        audit.record("job.rejected", f"Rejected: {job}", actor=actor, person=job.person, target=job)
        _settle(job)


def _locked_task(task: Task) -> Task:
    if task.person_id is not None:
        Person.objects.select_for_update().get(pk=task.person_id)
    return Task.objects.select_for_update().get(pk=task.pk)


def complete_task(task: Task, *, actor: User, note: str = "") -> None:
    with transaction.atomic():
        task = _locked_task(task)
        if task.status != Task.Status.OPEN:
            raise ActionError("This task is already closed.")
        _close_tasks(Task.objects.filter(pk=task.pk), Task.Status.DONE, actor=actor, note=note)
        job = task.job
        if job is not None and task.kind in (Task.Kind.CHANGE, Task.Kind.FAILURE) and job.is_open:
            job.status = Job.Status.SUCCEEDED
            job.finished_at = timezone.now()
            job.result = {**job.result, "manual": True, "completed_by": actor.email}
            job.save(update_fields=["status", "finished_at", "result"])
            accounts.record_manual_change(job)
            _settle(job)
        audit.record(
            "task.done",
            f"Completed task: {task.title}",
            actor=actor,
            person=task.person,
            target=task,
            details={"note": note[:500]} if note else {},
        )


def cancel_task(task: Task, *, actor: User, note: str = "") -> None:
    with transaction.atomic():
        task = _locked_task(task)
        if task.status != Task.Status.OPEN:
            raise ActionError("This task is already closed.")
        work = task.kind in (Task.Kind.CHANGE, Task.Kind.FAILURE)
        if work and task.job is not None and task.job.is_open:
            cancel_job(task.job, actor=actor, note=note or "Task cancelled.")
            _settle(task.job)
        else:
            _close_tasks(
                Task.objects.filter(pk=task.pk), Task.Status.CANCELLED, actor=actor, note=note
            )
        audit.record(
            "task.cancelled",
            f"Cancelled task: {task.title}",
            actor=actor,
            person=task.person,
            target=task,
            details={"note": note[:500]} if note else {},
        )


# -- email ---------------------------------------------------------------------


def email_connection() -> Connection | None:
    for connection in Connection.objects.filter(enabled=True).order_by("pk"):
        if connection.kind == AdapterKind.EMAIL:
            return connection
    return None


def send_email(to: list[str], subject: str, body: str) -> bool:
    connection = email_connection()
    if connection is None or not to:
        return False
    try:
        connection.build_adapter().send(to=to, subject=subject, body=body)
    except Exception as exc:  # noqa: BLE001 - email is best effort
        logger.warning("Email to %d recipient(s) failed: %s", len(to), type(exc).__name__)
        return False
    return True


def mask_email(address: str) -> str:
    """Enough of an address to notice a wrong recipient in the audit log,
    without keeping the private address there."""
    local, _, domain = address.partition("@")
    return f"{local[:1]}***@{domain}"


def hand_over_sign_in(job: Job, result: EnsureResult | DisableResult) -> None:
    """Get a new account's first sign-in to the person: email the initial
    password, or ask a person to arrange what the adapter could not."""
    if not isinstance(result, EnsureResult):
        return
    if result.initial_password:
        deliver_initial_password(job, result.initial_password)
    elif result.created and result.credentials_problem:
        _open_credentials_task(job, result.credentials_problem)


def _open_credentials_task(job: Job, instructions: str) -> None:
    person = job.person
    task = Task.objects.create(
        kind=Task.Kind.CREDENTIALS,
        title=f"Hand over {job.connection.name} sign-in details to {person.full_name}",
        instructions=instructions,
        person=person,
        job=job,
        connection=job.connection,
    )
    notify_after_commit([task])


def deliver_initial_password(job: Job, password: str) -> None:
    person = job.person
    organization = AppSettings.load().organization_name
    sent = bool(person.personal_email) and send_email(
        [person.personal_email],
        f"Your {job.connection.name} account at {organization}",
        "\n".join(
            [
                f"Hello {person.first_name},",
                "",
                f"Your {job.connection.name} account is ready.",
                f"Sign-in: {person.work_email or person.login_name}",
                f"Initial password: {password}",
                "",
                "You will be asked to choose a new password when you first sign in.",
            ]
        ),
    )
    if sent:
        audit.record(
            "credentials.sent",
            f"Emailed the initial {job.connection.name} password of {person.full_name} "
            f"to the personal address {mask_email(person.personal_email)}",
            person=person,
            target=job,
        )
        return
    _open_credentials_task(
        job,
        f"The app created the {job.connection.name} account but could not email the "
        "initial password (no email connection or no personal email address). The "
        f"password was not stored. Set a new temporary password in {job.connection.name} "
        "and give it to the person in a safe way.",
    )


def notify_after_commit(tasks: list[Task]) -> None:
    tasks = [task for task in tasks if task is not None]
    if not tasks:
        return
    transaction.on_commit(lambda: notify_tasks(tasks))


def notify_tasks(tasks: list[Task]) -> None:
    recipients = AppSettings.load().notification_recipients()
    if not recipients:
        return
    lines = ["New tasks in Meldgrove IAM:", ""]
    for task in tasks:
        lines.append(f"- {task.title}")
    send_email(recipients, f"{len(tasks)} new task(s) in Meldgrove IAM", "\n".join(lines))

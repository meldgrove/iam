from datetime import timedelta

import pytest
from django.utils import timezone

from iam.adapters.base import AdapterError, TransientError
from iam.jobs import (
    MAX_ATTEMPTS,
    ActionError,
    approve_job,
    cancel_task,
    claim_next_job,
    complete_task,
    recover_stale_jobs,
    reject_job,
    retry_job,
    run_job,
)
from iam.lifecycle import LifecycleError, cancel_workflow, start_workflow
from iam.models import (
    Account,
    AppSettings,
    AuditEvent,
    Connection,
    Job,
    Person,
    Task,
    User,
    Workflow,
)
from iam.policy import desired_access, managed_group_ids, snapshot

pytestmark = pytest.mark.django_db


def run_all():
    while (job := claim_next_job()) is not None:
        run_job(job)


def test_desired_access_combines_profile_and_team_groups(person, fake_connection):
    person.status = Person.Status.ACTIVE
    grant = desired_access(person)[fake_connection.pk]
    assert grant.group_ids == ["g-eng", "g-staff"]
    assert grant.reasons == {"g-staff": {"profile"}, "g-eng": {"team"}}
    assert managed_group_ids(fake_connection) == {"g-staff", "g-eng"}


def test_team_groups_need_an_account_from_the_profile(person, fake_connection):
    person.status = Person.Status.ACTIVE
    person.access_profile.grants.all().delete()
    assert desired_access(person) == {}


def test_people_without_access_should_have_nothing(person):
    for status in (Person.Status.PLANNED, Person.Status.OFFBOARDING, Person.Status.OFFBOARDED):
        person.status = status
        assert snapshot(person) == {}


def test_onboarding_creates_the_account_with_groups(person, fake_store, fake_connection):
    workflow = start_workflow(person, Workflow.Kind.ONBOARDING)
    person.refresh_from_db()
    assert person.status == Person.Status.ONBOARDING
    assert workflow.desired[str(fake_connection.pk)]["groups"] == ["g-eng", "g-staff"]
    run_all()
    workflow.refresh_from_db()
    person.refresh_from_db()
    assert workflow.status == Workflow.Status.COMPLETED
    assert person.status == Person.Status.ACTIVE
    account = Account.objects.get(person=person, connection=fake_connection)
    assert account.status == Account.Status.ACTIVE and account.created_by_app
    assert sorted(fake_store.accounts[account.external_id]["groups"]) == ["g-eng", "g-staff"]
    assert account.observed_groups == ["g-eng", "g-staff"]


def test_onboarding_needs_an_access_profile(person):
    person.access_profile = None
    person.save()
    with pytest.raises(LifecycleError, match="access profile"):
        start_workflow(person, Workflow.Kind.ONBOARDING)


def test_access_update_removes_only_managed_groups(person, fake_store, groups):
    start_workflow(person, Workflow.Kind.ONBOARDING)
    run_all()
    account = Account.objects.get(person=person)
    fake_store.accounts[account.external_id]["groups"].add("unmanaged")
    person.refresh_from_db()
    person.team = None
    person.save()
    start_workflow(person, Workflow.Kind.ACCESS_UPDATE)
    run_all()
    assert fake_store.accounts[account.external_id]["groups"] == {"g-staff", "unmanaged"}


def test_offboarding_disables_and_removes_managed_groups(person, fake_store):
    user = User.objects.create_user(email="ada@example.invalid", person=person)
    start_workflow(person, Workflow.Kind.ONBOARDING)
    run_all()
    workflow = start_workflow(person, Workflow.Kind.OFFBOARDING)
    user.refresh_from_db()
    assert not user.is_active, "offboarding turns off app access at once"
    run_all()
    workflow.refresh_from_db()
    person.refresh_from_db()
    account = Account.objects.get(person=person)
    assert workflow.status == Workflow.Status.COMPLETED
    assert person.status == Person.Status.OFFBOARDED
    assert account.status == Account.Status.DISABLED
    assert fake_store.accounts[account.external_id] == {
        "username": "ada",
        "email": "ada@example.invalid",
        "active": False,
        "groups": set(),
    }


def test_offboarding_reaches_profile_connections_without_known_accounts(person, fake_store):
    person.status = Person.Status.ACTIVE
    person.save()
    workflow = start_workflow(person, Workflow.Kind.OFFBOARDING)
    assert workflow.jobs.get().action == Job.Action.DISABLE_ACCOUNT


def test_offboarding_checks_an_account_a_comparison_missed(
    person, fake_store, fake_connection, profile
):
    # An account the profile does not grant, linked by a comparison. A later
    # listing missed it (a page that shifted, an empty answer): it still exists.
    fake_store.accounts["x1"] = {
        "username": "ada",
        "email": "ada@example.invalid",
        "active": True,
        "groups": set(),
    }
    profile.grants.all().delete()
    Account.objects.create(
        connection=fake_connection,
        person=person,
        external_id="x1",
        status=Account.Status.MISSING,
    )
    person.status = Person.Status.ACTIVE
    person.save()
    workflow = start_workflow(person, Workflow.Kind.OFFBOARDING)
    assert workflow.jobs.get().action == Job.Action.DISABLE_ACCOUNT
    run_all()
    assert fake_store.accounts["x1"]["active"] is False
    # An access update leaves accounts last seen missing to the comparison.
    Account.objects.filter(person=person).update(status=Account.Status.MISSING)
    person.status = Person.Status.ACTIVE
    person.save()
    assert not start_workflow(person, Workflow.Kind.ACCESS_UPDATE).jobs.exists()


def test_new_workflow_replaces_the_open_one(person):
    first = start_workflow(person, Workflow.Kind.ONBOARDING)
    second = start_workflow(person, Workflow.Kind.OFFBOARDING)
    first.refresh_from_db()
    assert first.status == Workflow.Status.CANCELLED
    assert first.jobs.get().status == Job.Status.CANCELLED
    assert second.is_open


def test_transient_errors_retry_with_backoff(person, fake_store):
    start_workflow(person, Workflow.Kind.ONBOARDING)
    fake_store.fail_with = TransientError("Fake app answered 503.")
    job = claim_next_job()
    run_job(job)
    job.refresh_from_db()
    assert job.status == Job.Status.QUEUED
    assert job.next_attempt_at > timezone.now()
    assert job.last_error == "Fake app answered 503."
    assert not Task.objects.exists()


def test_last_retry_hands_the_job_to_a_person(person, fake_store):
    start_workflow(person, Workflow.Kind.ONBOARDING)
    fake_store.fail_with = TransientError("timeout")
    job = Job.objects.get()
    job.attempts = MAX_ATTEMPTS - 1
    job.save()
    run_job(claim_next_job())
    job.refresh_from_db()
    assert job.status == Job.Status.FAILED
    task = Task.objects.get(job=job)
    assert task.kind == Task.Kind.FAILURE and "timeout" in task.instructions
    assert job.workflow.status == Workflow.Status.FAILED


def test_permanent_errors_fail_at_once_and_retry_recovers(person, fake_store, approver_user):
    start_workflow(person, Workflow.Kind.ONBOARDING)
    fake_store.fail_with = AdapterError("Fake app answered 400.")
    run_job(claim_next_job())
    job = Job.objects.get()
    assert job.status == Job.Status.FAILED
    fake_store.fail_with = None
    retry_job(job, actor=approver_user)
    run_all()
    job.refresh_from_db()
    assert job.status == Job.Status.SUCCEEDED
    assert Task.objects.get(job=job).status == Task.Status.CANCELLED
    assert job.workflow.status == Workflow.Status.COMPLETED


def test_unexpected_errors_do_not_leak_details(person, fake_store):
    start_workflow(person, Workflow.Kind.ONBOARDING)
    fake_store.fail_with = RuntimeError("token=supersecret")
    run_job(claim_next_job())
    job = Job.objects.get()
    assert job.status == Job.Status.FAILED
    assert "supersecret" not in job.last_error
    assert "supersecret" not in Task.objects.get().instructions


def test_manual_connections_create_tasks_and_completion_records_the_change(
    person, fake_connection, approver_user
):
    fake_connection.mode = Connection.Mode.MANUAL
    fake_connection.save()
    workflow = start_workflow(person, Workflow.Kind.ONBOARDING)
    job = workflow.jobs.get()
    assert job.status == Job.Status.MANUAL
    task = Task.objects.get(job=job, kind=Task.Kind.CHANGE)
    assert "Engineering" in task.instructions and "Staff" in task.instructions
    workflow.refresh_from_db()
    assert workflow.status == Workflow.Status.WAITING
    complete_task(task, actor=approver_user, note="Invited")
    job.refresh_from_db()
    workflow.refresh_from_db()
    person.refresh_from_db()
    assert job.status == Job.Status.SUCCEEDED and job.result["manual"]
    assert workflow.status == Workflow.Status.COMPLETED
    assert person.status == Person.Status.ACTIVE
    assert Account.objects.get(person=person).status == Account.Status.ACTIVE


def test_cancelling_a_manual_task_drops_the_change(person, fake_connection, approver_user):
    fake_connection.mode = Connection.Mode.MANUAL
    fake_connection.save()
    workflow = start_workflow(person, Workflow.Kind.ONBOARDING)
    task = Task.objects.get(job__workflow=workflow, kind=Task.Kind.CHANGE)
    cancel_task(task, actor=approver_user, note="Not needed")
    task.refresh_from_db()
    workflow.refresh_from_db()
    assert task.status == Task.Status.CANCELLED and task.completed_by == approver_user
    assert workflow.jobs.get().status == Job.Status.CANCELLED
    assert workflow.status == Workflow.Status.COMPLETED
    assert not Account.objects.filter(person=person).exists()
    with pytest.raises(ActionError):
        cancel_task(task, actor=approver_user)


def test_disabled_connections_never_change_silently(person, fake_connection, fake_store):
    start_workflow(person, Workflow.Kind.ONBOARDING)
    run_all()
    fake_connection.mode = Connection.Mode.READ_ONLY
    fake_connection.save()
    workflow = start_workflow(person, Workflow.Kind.OFFBOARDING)
    job = workflow.jobs.get()
    assert job.status == Job.Status.MANUAL
    assert "read only" in Task.objects.get(job=job).instructions
    assert all(data["active"] for data in fake_store.accounts.values())


def test_automatic_removals_over_the_limit_wait_for_approval(person, fake_store, admin_user):
    settings = AppSettings.load()
    settings.removal_limit_per_day = 0
    settings.save()
    start_workflow(person, Workflow.Kind.ONBOARDING)
    run_all()
    workflow = start_workflow(person, Workflow.Kind.OFFBOARDING, automatic=True)
    job = workflow.jobs.get()
    assert job.status == Job.Status.HELD
    assert claim_next_job() is None
    approve_job(job, actor=admin_user)
    run_all()
    job.refresh_from_db()
    assert job.status == Job.Status.SUCCEEDED


def test_rejecting_a_held_removal_keeps_the_account(person, fake_store, admin_user):
    AppSettings.objects.update_or_create(pk=1, defaults={"removal_limit_per_day": 0})
    start_workflow(person, Workflow.Kind.ONBOARDING)
    run_all()
    job = start_workflow(person, Workflow.Kind.OFFBOARDING, automatic=True).jobs.get()
    reject_job(job, actor=admin_user)
    job.refresh_from_db()
    assert job.status == Job.Status.CANCELLED
    assert all(data["active"] for data in fake_store.accounts.values())
    with pytest.raises(ActionError):
        approve_job(job, actor=admin_user)


def test_operator_removals_are_never_held(person, fake_store, approver_user):
    AppSettings.objects.update_or_create(pk=1, defaults={"removal_limit_per_day": 0})
    start_workflow(person, Workflow.Kind.ONBOARDING)
    run_all()
    job = start_workflow(person, Workflow.Kind.OFFBOARDING, actor=approver_user).jobs.get()
    assert job.status == Job.Status.QUEUED


def test_stale_running_jobs_go_back_to_the_queue(person):
    start_workflow(person, Workflow.Kind.ONBOARDING)
    job = claim_next_job()
    Job.objects.filter(pk=job.pk).update(locked_until=timezone.now() - timedelta(seconds=1))
    assert recover_stale_jobs() == 1
    job.refresh_from_db()
    assert job.status == Job.Status.QUEUED


def test_initial_password_is_emailed_never_stored(person, fake_store, monkeypatch):
    from tests.fakes import FakeAdapter

    FakeAdapter.password = "Initial-Pass-123"
    sent = []
    monkeypatch.setattr(
        "iam.jobs.send_email", lambda to, subject, body: sent.append((to, body)) or True
    )
    start_workflow(person, Workflow.Kind.ONBOARDING)
    run_all()
    assert sent and sent[0][0] == ["ada@personal.example.invalid"]
    assert "Initial-Pass-123" in sent[0][1]
    job = Job.objects.get()
    assert job.result["password_generated"] is True
    stored = [str(v) for v in Job.objects.values_list("result", "last_error")]
    stored += list(AuditEvent.objects.values_list("summary", flat=True))
    stored += [str(d) for d in AuditEvent.objects.values_list("details", flat=True)]
    assert not any("Initial-Pass-123" in value for value in stored)
    # The audit log shows where it went, without keeping the private address.
    summary = AuditEvent.objects.get(action="credentials.sent").summary
    assert "a***@personal.example.invalid" in summary and "ada@" not in summary


def test_approvers_cannot_offboard_an_admin_of_this_app(person, approver_user, admin_user):
    # Offboarding turns the app user off: an approver could lock every admin out.
    admin_user.person = person
    admin_user.save()
    person.status = Person.Status.ACTIVE
    person.save()
    with pytest.raises(LifecycleError, match="is an admin of this app"):
        start_workflow(person, Workflow.Kind.OFFBOARDING, actor=approver_user)
    admin_user.refresh_from_db()
    person.refresh_from_db()
    assert admin_user.is_active and person.status == Person.Status.ACTIVE
    # Another admin may, and so may the schedule after the last day.
    start_workflow(person, Workflow.Kind.OFFBOARDING, actor=admin_user)
    admin_user.refresh_from_db()
    assert not admin_user.is_active


def test_undeliverable_password_becomes_a_task(person, fake_store):
    from tests.fakes import FakeAdapter

    FakeAdapter.password = "Initial-Pass-123"
    start_workflow(person, Workflow.Kind.ONBOARDING)
    run_all()
    task = Task.objects.get(kind=Task.Kind.CREDENTIALS)
    assert "Initial-Pass-123" not in task.instructions


def test_account_linked_to_someone_else_is_a_conflict(person, fake_store, fake_connection, profile):
    other = Person.objects.create(first_name="Ada", last_name="Other", access_profile=profile)
    start_workflow(person, Workflow.Kind.ONBOARDING)
    run_all()
    external_id = Account.objects.get(person=person).external_id
    Account.objects.filter(person=person).update(person=other)
    workflow = start_workflow(person, Workflow.Kind.ACCESS_UPDATE)
    run_all()
    job = workflow.jobs.get()
    assert job.status == Job.Status.FAILED
    assert "belongs to" in job.last_error
    assert Account.objects.get(external_id=external_id).person == other


def test_cancel_onboarding_returns_to_planned_but_offboarding_cannot_be_cancelled(
    person, admin_user
):
    workflow = start_workflow(person, Workflow.Kind.ONBOARDING)
    cancel_workflow(workflow, actor=admin_user)
    person.refresh_from_db()
    assert person.status == Person.Status.PLANNED
    offboarding = start_workflow(person, Workflow.Kind.OFFBOARDING)
    with pytest.raises(LifecycleError):
        cancel_workflow(offboarding, actor=admin_user)


def test_status_rules(person):
    with pytest.raises(LifecycleError):
        start_workflow(person, Workflow.Kind.ACCESS_UPDATE)
    start_workflow(person, Workflow.Kind.ONBOARDING)
    # An onboarding restarts only through an access update.
    with pytest.raises(LifecycleError):
        start_workflow(person, Workflow.Kind.ONBOARDING)


def test_offboarding_reaches_turned_off_connections_as_tasks(person, fake_connection, fake_store):
    start_workflow(person, Workflow.Kind.ONBOARDING)
    run_all()
    fake_connection.enabled = False
    fake_connection.save()
    workflow = start_workflow(person, Workflow.Kind.OFFBOARDING)
    job = workflow.jobs.get()
    assert job.action == Job.Action.DISABLE_ACCOUNT and job.status == Job.Status.MANUAL
    assert "turned off" in Task.objects.get(job=job).instructions
    assert all(data["active"] for data in fake_store.accounts.values())


def test_access_updates_skip_turned_off_connections(person, fake_connection, fake_store):
    start_workflow(person, Workflow.Kind.ONBOARDING)
    run_all()
    fake_connection.enabled = False
    fake_connection.save()
    workflow = start_workflow(person, Workflow.Kind.ACCESS_UPDATE)
    assert not workflow.jobs.exists()


def test_manual_tasks_name_the_groups_to_remove(person, fake_connection, groups):
    fake_connection.mode = Connection.Mode.MANUAL
    fake_connection.save()
    from iam.models import Team

    # Another team makes Operations a managed group that Ada should not have.
    Team.objects.create(name="Ops").groups.add(groups["g-ops"])
    start_workflow(person, Workflow.Kind.ONBOARDING)
    task = Task.objects.get(kind=Task.Kind.CHANGE)
    assert "Remove from these, if a member: Operations." in task.instructions


def test_a_replaced_running_job_never_runs_again(person, fake_store):
    start_workflow(person, Workflow.Kind.ONBOARDING)
    old = claim_next_job()
    offboarding = start_workflow(person, Workflow.Kind.OFFBOARDING)
    new = offboarding.jobs.get()
    assert claim_next_job() is None, "waits while the old job for the same app runs"
    fake_store.fail_with = TransientError("timeout")
    run_job(old)
    old.refresh_from_db()
    assert old.status == Job.Status.CANCELLED
    fake_store.fail_with = None
    run_all()
    new.refresh_from_db()
    assert new.status == Job.Status.SUCCEEDED
    assert claim_next_job() is None


def test_someone_elses_account_is_never_changed(person, fake_store, profile):
    start_workflow(person, Workflow.Kind.ONBOARDING)
    run_all()
    namesake = Person.objects.create(
        first_name="Ada",
        last_name="Namesake",
        username="ada",
        access_profile=profile,
        status=Person.Status.ACTIVE,
    )
    job = start_workflow(namesake, Workflow.Kind.OFFBOARDING).jobs.get()
    run_all()
    job.refresh_from_db()
    assert job.status == Job.Status.FAILED and "belongs to Ada Lovelace" in job.last_error
    assert all(data["active"] for data in fake_store.accounts.values())
    assert "active:" not in " ".join(fake_store.calls)


def test_initial_password_survives_a_failed_group_step(person, fake_store, monkeypatch):
    from tests.fakes import FakeAdapter

    FakeAdapter.password = "Initial-Pass-123"
    sent = []
    monkeypatch.setattr("iam.jobs.send_email", lambda to, subject, body: sent.append(body) or True)
    fake_store.fail_group_with = TransientError("Fake app answered 503.")
    start_workflow(person, Workflow.Kind.ONBOARDING)
    run_all()
    job = Job.objects.get()
    assert job.status == Job.Status.QUEUED and len(sent) == 1
    assert "Initial-Pass-123" in sent[0]
    Job.objects.filter(pk=job.pk).update(next_attempt_at=timezone.now())
    run_all()
    job.refresh_from_db()
    assert job.status == Job.Status.SUCCEEDED and len(sent) == 1


GROUPS_UNREADABLE = "Fake app answered 404 while trying to list the user's groups."


def test_a_failing_group_never_blocks_offboarding(person, fake_store):
    start_workflow(person, Workflow.Kind.ONBOARDING)
    run_all()
    fake_store.fail_group_read_with = AdapterError(GROUPS_UNREADABLE)
    workflow = start_workflow(person, Workflow.Kind.OFFBOARDING)
    run_all()
    workflow.refresh_from_db()
    person.refresh_from_db()
    job = workflow.jobs.get()
    account = Account.objects.get(person=person)
    assert fake_store.accounts[account.external_id]["active"] is False
    assert job.status == Job.Status.SUCCEEDED
    assert workflow.status == Workflow.Status.COMPLETED
    assert person.status == Person.Status.OFFBOARDED
    assert account.status == Account.Status.DISABLED
    assert account.applied_groups == ["g-eng", "g-staff"], "still the app's to remove"
    assert job.result["group_error"] == GROUPS_UNREADABLE
    task = Task.objects.get(job=job, status=Task.Status.OPEN)
    assert task.kind == Task.Kind.FAILURE
    assert GROUPS_UNREADABLE in task.instructions
    assert "Engineering, Staff" in task.instructions


def test_a_failing_group_never_blocks_onboarding(person, fake_store, monkeypatch):
    from tests.fakes import FakeAdapter

    FakeAdapter.password = "Initial-Pass-123"
    sent = []
    monkeypatch.setattr("iam.jobs.send_email", lambda to, subject, body: sent.append(body) or True)
    fake_store.fail_group_read_with = AdapterError(GROUPS_UNREADABLE)
    workflow = start_workflow(person, Workflow.Kind.ONBOARDING)
    run_all()
    workflow.refresh_from_db()
    person.refresh_from_db()
    job = workflow.jobs.get()
    account = Account.objects.get(person=person)
    assert [body for body in sent if "Initial-Pass-123" in body] == sent[:1]
    assert job.status == Job.Status.SUCCEEDED
    assert workflow.status == Workflow.Status.COMPLETED
    assert person.status == Person.Status.ACTIVE
    assert account.status == Account.Status.ACTIVE
    assert account.observed_groups is None, "groups the app could not read are not claimed"
    task = Task.objects.get(job=job, status=Task.Status.OPEN)
    assert task.kind == Task.Kind.FAILURE and GROUPS_UNREADABLE in task.instructions
    assert "Engineering, Staff" in task.instructions


def test_a_newer_workflow_replaces_an_unfinished_group_task(person, fake_store):
    fake_store.fail_group_read_with = AdapterError(GROUPS_UNREADABLE)
    start_workflow(person, Workflow.Kind.ONBOARDING)
    run_all()
    task = Task.objects.get(kind=Task.Kind.FAILURE, status=Task.Status.OPEN)
    fake_store.fail_group_read_with = None
    workflow = start_workflow(person, Workflow.Kind.OFFBOARDING)
    run_all()
    task.refresh_from_db()
    workflow.refresh_from_db()
    assert task.status == Task.Status.CANCELLED, "never add an offboarded person to groups"
    assert workflow.status == Workflow.Status.COMPLETED
    assert not Task.objects.filter(status=Task.Status.OPEN).exists()


def test_a_replaced_running_job_opens_no_group_task(person, fake_store):
    start_workflow(person, Workflow.Kind.ONBOARDING)
    running = claim_next_job()
    offboarding = start_workflow(person, Workflow.Kind.OFFBOARDING)
    fake_store.fail_group_read_with = AdapterError(GROUPS_UNREADABLE)
    run_job(running)
    fake_store.fail_group_read_with = None
    run_all()
    offboarding.refresh_from_db()
    person.refresh_from_db()
    assert offboarding.status == Workflow.Status.COMPLETED
    assert person.status == Person.Status.OFFBOARDED
    assert not Task.objects.filter(status=Task.Status.OPEN).exists()


def test_passing_group_errors_retry_before_they_become_a_task(person, fake_store):
    fake_store.fail_group_read_with = TransientError("Fake app answered 503.")
    workflow = start_workflow(person, Workflow.Kind.ONBOARDING)
    run_all()
    job = workflow.jobs.get()
    assert job.status == Job.Status.QUEUED and job.last_error == "Fake app answered 503."
    assert not Task.objects.exists()
    account = Account.objects.get(person=person)
    assert account.status == Account.Status.ACTIVE and account.created_by_app
    assert AuditEvent.objects.get(action="job.ensure_account").summary.startswith("Created")
    Job.objects.filter(pk=job.pk).update(attempts=MAX_ATTEMPTS - 1, next_attempt_at=timezone.now())
    run_all()
    job.refresh_from_db()
    workflow.refresh_from_db()
    account.refresh_from_db()
    assert len(fake_store.accounts) == 1
    assert account.created_by_app
    assert job.status == Job.Status.SUCCEEDED
    assert workflow.status == Workflow.Status.COMPLETED
    assert Task.objects.get(job=job).kind == Task.Kind.FAILURE


def test_an_account_disabled_before_a_group_retry_is_audited(person, fake_store, profile):
    start_workflow(person, Workflow.Kind.ONBOARDING)
    run_all()
    fake_store.fail_group_read_with = TransientError("Fake app answered 503.")
    job = start_workflow(person, Workflow.Kind.OFFBOARDING).jobs.get()
    run_all()
    job.refresh_from_db()
    account = Account.objects.get(person=person)
    assert job.status == Job.Status.QUEUED
    assert fake_store.accounts[account.external_id]["active"] is False
    event = AuditEvent.objects.get(action="job.disable_account")
    assert event.summary.startswith("Disabled the Fake app account")
    assert event.details["group_error"] == "Fake app answered 503."
    # A newer offboarding without the app replaces the retry, and still
    # removes the groups.
    fake_store.fail_group_read_with = None
    profile.grants.all().delete()
    workflow = start_workflow(person, Workflow.Kind.OFFBOARDING)
    run_all()
    account.refresh_from_db()
    assert workflow.jobs.get().status == Job.Status.SUCCEEDED
    assert account.status == Account.Status.DISABLED and account.applied_groups == []
    assert fake_store.accounts[account.external_id]["groups"] == set()


def test_a_group_task_stays_open_where_no_newer_job_runs(person, fake_store, profile):
    start_workflow(person, Workflow.Kind.ONBOARDING)
    run_all()
    fake_store.fail_group_read_with = AdapterError(GROUPS_UNREADABLE)
    start_workflow(person, Workflow.Kind.OFFBOARDING)
    run_all()
    fake_store.fail_group_read_with = None
    task = Task.objects.get(kind=Task.Kind.FAILURE, status=Task.Status.OPEN)
    # Hired again with a profile that no longer has this app.
    profile.grants.all().delete()
    rehire = start_workflow(person, Workflow.Kind.ONBOARDING)
    run_all()
    task.refresh_from_db()
    assert not rehire.jobs.exists()
    assert task.status == Task.Status.OPEN, "nothing else removes the groups"


def test_unexpected_group_errors_do_not_leak_details(person, fake_store):
    fake_store.fail_group_read_with = RuntimeError("token=supersecret")
    start_workflow(person, Workflow.Kind.ONBOARDING)
    run_all()
    job = Job.objects.get()
    assert job.status == Job.Status.SUCCEEDED
    assert "supersecret" not in str(job.result)
    assert "supersecret" not in Task.objects.get(job=job).instructions


def test_groups_dropped_from_the_policy_are_removed(person, fake_store, profile):
    start_workflow(person, Workflow.Kind.ONBOARDING)
    run_all()
    account = Account.objects.get(person=person)
    assert account.applied_groups == ["g-eng", "g-staff"]
    fake_store.accounts[account.external_id]["groups"].add("unmanaged")
    profile.grants.get().groups.clear()
    start_workflow(person, Workflow.Kind.ACCESS_UPDATE)
    run_all()
    assert fake_store.accounts[account.external_id]["groups"] == {"g-eng", "unmanaged"}
    account.refresh_from_db()
    assert account.applied_groups == ["g-eng"]


def test_groups_applied_by_a_still_running_job_are_removed_later(person, fake_store, profile):
    start_workflow(person, Workflow.Kind.ONBOARDING)
    running = claim_next_job()
    profile.grants.get().groups.clear()
    replacement = start_workflow(person, Workflow.Kind.ACCESS_UPDATE).jobs.get()
    assert "g-staff" not in replacement.managed_groups
    run_job(running)
    run_all()
    replacement.refresh_from_db()
    assert replacement.status == Job.Status.SUCCEEDED
    account = Account.objects.get(person=person)
    assert fake_store.accounts[account.external_id]["groups"] == {"g-eng"}
    assert account.applied_groups == ["g-eng"]


def test_applying_access_during_onboarding_still_ends_active(person, fake_store):
    start_workflow(person, Workflow.Kind.ONBOARDING)
    workflow = start_workflow(person, Workflow.Kind.ACCESS_UPDATE)
    assert workflow.kind == Workflow.Kind.ONBOARDING
    run_all()
    person.refresh_from_db()
    assert person.status == Person.Status.ACTIVE


def test_cancelling_a_workflow_keeps_the_result_of_its_running_job(
    person, fake_store, admin_user, monkeypatch
):
    from tests.fakes import FakeAdapter

    FakeAdapter.password = "Initial-Pass-123"
    sent = []
    monkeypatch.setattr("iam.jobs.send_email", lambda to, subject, body: sent.append(body) or True)
    workflow = start_workflow(person, Workflow.Kind.ONBOARDING)
    job = claim_next_job()
    cancel_workflow(workflow, actor=admin_user)
    run_job(job)
    job.refresh_from_db()
    workflow.refresh_from_db()
    assert job.status == Job.Status.SUCCEEDED
    assert workflow.status == Workflow.Status.CANCELLED
    assert Account.objects.get(person=person).status == Account.Status.ACTIVE
    assert len(sent) == 1 and "Initial-Pass-123" in sent[0]


def test_a_job_cancelled_while_it_ran_still_records_its_change(person, fake_store, monkeypatch):
    from iam.jobs import cancel_job
    from tests.fakes import FakeAdapter

    FakeAdapter.password = "Initial-Pass-123"
    sent = []
    monkeypatch.setattr("iam.jobs.send_email", lambda to, subject, body: sent.append(body) or True)
    start_workflow(person, Workflow.Kind.ONBOARDING)
    job = claim_next_job()
    cancel_job(Job.objects.get(pk=job.pk), note="Cancelled elsewhere.")
    run_job(job)
    assert Account.objects.get(person=person).status == Account.Status.ACTIVE
    assert len(sent) == 1 and "Initial-Pass-123" in sent[0]


def test_job_locks_never_cover_joined_rows(person, admin_user):
    from django.db import connection
    from django.test.utils import CaptureQueriesContext

    if connection.vendor != "postgresql":
        pytest.skip("Only PostgreSQL locks rows.")
    workflow = start_workflow(person, Workflow.Kind.ONBOARDING)
    with CaptureQueriesContext(connection) as queries:
        claim_next_job()
        start_workflow(person, Workflow.Kind.ACCESS_UPDATE)  # restarts the onboarding
        cancel_workflow(Workflow.objects.latest("pk"), actor=admin_user)
    locking = [q["sql"] for q in queries if "FOR UPDATE" in q["sql"] and " JOIN " in q["sql"]]
    assert locking, workflow
    assert all('FOR UPDATE OF "iam_job"' in sql for sql in locking), locking


def test_a_job_changes_the_account_it_checked(person, fake_store):
    start_workflow(person, Workflow.Kind.ONBOARDING)
    run_all()
    assert fake_store.calls.count("find") == 1


@pytest.mark.django_db(transaction=True)
def test_jobs_that_finish_at_once_still_complete_the_workflow(person, profile, monkeypatch):
    import threading

    from django.db import connection, connections

    from iam import jobs
    from iam.adapters.base import EnsureResult, ExternalAccount
    from iam.models import ProfileGrant

    if connection.vendor != "postgresql":
        pytest.skip("Only PostgreSQL runs transactions side by side.")
    second = Connection(adapter="fake", name="Second app", enabled=True, settings={})
    second.set_secrets({"token": "fake-token"})
    second.save()
    ProfileGrant.objects.create(profile=profile, connection=second)
    workflow = start_workflow(person, Workflow.Kind.ONBOARDING)
    both = list(workflow.jobs.order_by("pk"))
    Job.objects.filter(workflow=workflow).update(status=Job.Status.RUNNING)
    # Both transactions have saved their job before either works out the workflow.
    barrier = threading.Barrier(2, timeout=5)
    original = jobs.refresh_workflow

    def refresh_after_both_saved(workflow):
        try:
            barrier.wait()
        except threading.BrokenBarrierError:
            pass  # the lock already makes the second transaction wait
        return original(workflow)

    monkeypatch.setattr(jobs, "refresh_workflow", refresh_after_both_saved)

    def finish(job):
        try:
            account = ExternalAccount(external_id=f"x{job.pk}", username=f"x{job.pk}")
            jobs.succeed_job(Job.objects.get(pk=job.pk), EnsureResult(account=account))
        finally:
            connections.close_all()

    threads = [threading.Thread(target=finish, args=(job,)) for job in both]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    workflow.refresh_from_db()
    person.refresh_from_db()
    assert workflow.status == Workflow.Status.COMPLETED
    assert person.status == Person.Status.ACTIVE


def test_the_schedule_completes_workflows_left_open(person, fake_store):
    from iam.schedule import run_schedule

    workflow = start_workflow(person, Workflow.Kind.ONBOARDING)
    run_all()
    # As two job changes that committed side by side used to leave it.
    Workflow.objects.filter(pk=workflow.pk).update(status=Workflow.Status.RUNNING)
    Person.objects.filter(pk=person.pk).update(status=Person.Status.ONBOARDING)
    assert run_schedule()["settled"] == 1
    workflow.refresh_from_db()
    person.refresh_from_db()
    assert workflow.status == Workflow.Status.COMPLETED
    assert person.status == Person.Status.ACTIVE


def test_a_worker_whose_lease_ran_out_leaves_the_new_claim_alone(person, fake_store):
    start_workflow(person, Workflow.Kind.ONBOARDING)
    first = claim_next_job()
    Job.objects.filter(pk=first.pk).update(locked_until=timezone.now() - timedelta(seconds=1))
    recover_stale_jobs()
    second = claim_next_job()
    assert second.pk == first.pk
    # The first worker comes back: its failure must not requeue the second claim.
    fake_store.fail_with = TransientError("Fake app answered 503.")
    run_job(first)
    job = Job.objects.get(pk=first.pk)
    assert job.status == Job.Status.RUNNING and job.locked_until == second.locked_until
    # Its success records the account, but the second claim settles the job.
    fake_store.fail_with = None
    run_job(first)
    job.refresh_from_db()
    assert job.status == Job.Status.RUNNING
    assert Account.objects.get(person=person).status == Account.Status.ACTIVE
    run_job(second)
    job.refresh_from_db()
    assert job.status == Job.Status.SUCCEEDED


def test_a_replaced_job_at_a_turned_off_connection_opens_no_task(
    person, fake_connection, fake_store
):
    start_workflow(person, Workflow.Kind.ONBOARDING)
    job = claim_next_job()
    start_workflow(person, Workflow.Kind.OFFBOARDING)
    Connection.objects.filter(pk=fake_connection.pk).update(enabled=False)
    run_job(job)
    job.refresh_from_db()
    assert job.status == Job.Status.CANCELLED
    assert not Task.objects.filter(job=job).exists()


def test_offboarding_waits_for_an_account_a_replaced_job_still_creates(
    person, fake_connection, fake_store, profile
):
    start_workflow(person, Workflow.Kind.ONBOARDING)
    creating = claim_next_job()
    profile.grants.all().delete()
    offboarding = start_workflow(person, Workflow.Kind.OFFBOARDING)
    removal = offboarding.jobs.get()
    assert removal.action == Job.Action.DISABLE_ACCOUNT
    assert claim_next_job() is None, "waits while the replaced job runs"
    run_job(creating)
    run_all()
    removal.refresh_from_db()
    assert removal.status == Job.Status.SUCCEEDED
    assert Account.objects.get(person=person).status == Account.Status.DISABLED


def test_a_late_worker_never_undoes_what_the_new_claim_settled(person, fake_store):
    start_workflow(person, Workflow.Kind.ONBOARDING)
    first = claim_next_job()
    Job.objects.filter(pk=first.pk).update(locked_until=timezone.now() - timedelta(seconds=1))
    recover_stale_jobs()
    second = claim_next_job()
    run_job(second)
    job = Job.objects.get(pk=first.pk)
    assert job.status == Job.Status.SUCCEEDED
    settled = (job.result, job.finished_at)
    fake_store.fail_with = AdapterError("Fake app answered 400.")
    run_job(first)
    job.refresh_from_db()
    assert job.status == Job.Status.SUCCEEDED and not Task.objects.exists()
    fake_store.fail_with = None
    audited = AuditEvent.objects.filter(action="job.ensure_account").count()
    run_job(first)
    job.refresh_from_db()
    assert (job.result, job.finished_at) == settled
    # The late change is still audited, as every change by the worker.
    assert AuditEvent.objects.filter(action="job.ensure_account").count() == audited + 1


def test_job_changes_lock_the_person_first(person, fake_connection, fake_store, admin_user):
    from django.db import connection
    from django.test.utils import CaptureQueriesContext

    from iam.jobs import fail_job

    if connection.vendor != "postgresql":
        pytest.skip("Only PostgreSQL locks rows.")

    def first_lock(action):
        with CaptureQueriesContext(connection) as queries:
            action()
        locks = [q["sql"] for q in queries if " FOR " in q["sql"] and "UPDATE" in q["sql"]]
        return locks[0].split(" FROM ")[1].split()[0] if locks else None

    start_workflow(person, Workflow.Kind.ONBOARDING)
    job = claim_next_job()
    assert first_lock(lambda: fail_job(job, "Fake app answered 400.", retryable=False)) == (
        '"iam_person"'
    )
    assert first_lock(lambda: retry_job(job, actor=admin_user)) == '"iam_person"'
    Connection.objects.filter(pk=fake_connection.pk).update(mode=Connection.Mode.MANUAL)
    job = claim_next_job()
    assert first_lock(lambda: run_job(job)) == '"iam_person"'
    task = Task.objects.get(job=job, status=Task.Status.OPEN)
    assert first_lock(lambda: complete_task(task, actor=admin_user)) == '"iam_person"'


def test_groups_marked_missing_are_left_alone(person, fake_store, groups):
    start_workflow(person, Workflow.Kind.ONBOARDING)
    run_all()
    account = Account.objects.get(person=person)
    assert fake_store.accounts[account.external_id]["groups"] == {"g-staff", "g-eng"}
    # An import that read nothing marked every group missing.
    for group in groups.values():
        group.missing = True
        group.save()
    start_workflow(person, Workflow.Kind.ACCESS_UPDATE)
    run_all()
    assert fake_store.accounts[account.external_id]["groups"] == {"g-staff", "g-eng"}
    account.refresh_from_db()
    assert account.applied_groups == ["g-eng", "g-staff"]


def test_sign_in_the_adapter_could_not_arrange_becomes_a_task(person, fake_store):
    from tests.fakes import FakeAdapter

    FakeAdapter.credentials_problem = "The setup email was not sent. Send it by hand."
    start_workflow(person, Workflow.Kind.ONBOARDING)
    run_all()
    task = Task.objects.get(kind=Task.Kind.CREDENTIALS)
    assert task.instructions == "The setup email was not sent. Send it by hand."


def test_a_cancelled_onboarding_waits_for_a_new_start_date(person, approver_user):
    from iam.schedule import start_due_onboarding

    Person.objects.filter(pk=person.pk).update(start_date=timezone.localdate())
    [workflow] = start_due_onboarding()
    cancel_workflow(workflow, actor=approver_user)
    person.refresh_from_db()
    assert person.status == Person.Status.PLANNED and person.onboarding_paused
    assert start_due_onboarding() == []
    # Starting it by hand is still possible, and lifts the pause.
    start_workflow(person, Workflow.Kind.ONBOARDING, actor=approver_user)
    person.refresh_from_db()
    assert person.status == Person.Status.ONBOARDING and not person.onboarding_paused


def test_onboarding_refuses_a_last_day_that_has_passed(person, approver_user):
    Person.objects.filter(pk=person.pk).update(
        status=Person.Status.OFFBOARDED, end_date=timezone.localdate() - timedelta(days=1)
    )
    person.refresh_from_db()
    with pytest.raises(LifecycleError, match="has passed"):
        start_workflow(person, Workflow.Kind.ONBOARDING, actor=approver_user)
    Person.objects.filter(pk=person.pk).update(end_date=None)
    person.refresh_from_db()
    assert start_workflow(person, Workflow.Kind.ONBOARDING, actor=approver_user)


def test_sign_in_details_survive_a_newer_workflow(person, fake_store, approver_user):
    from tests.fakes import FakeAdapter

    FakeAdapter.password = "Initial-Pass-123"
    # No email connection: a task hands over the details. A passing group
    # error keeps the job queued, so the newer workflow cancels it.
    fake_store.fail_group_with = TransientError("Fake app answered 503.")
    start_workflow(person, Workflow.Kind.ONBOARDING, actor=approver_user)
    run_all()
    credentials = Task.objects.get(kind=Task.Kind.CREDENTIALS)
    start_workflow(person, Workflow.Kind.ACCESS_UPDATE, actor=approver_user)
    run_all()
    credentials.refresh_from_db()
    assert credentials.status == Task.Status.OPEN


def test_retrying_a_job_keeps_its_sign_in_details(person, fake_store, approver_user):
    from tests.fakes import FakeAdapter

    FakeAdapter.password = "Initial-Pass-123"
    fake_store.fail_group_with = TransientError("Fake app answered 503.")
    start_workflow(person, Workflow.Kind.ONBOARDING, actor=approver_user)
    run_all()
    job = Job.objects.get()
    fake_store.fail_with = AdapterError("Fake app answered 400.")
    Job.objects.filter(pk=job.pk).update(next_attempt_at=timezone.now())
    run_all()
    job.refresh_from_db()
    assert job.status == Job.Status.FAILED
    retry_job(job, actor=approver_user)
    credentials = Task.objects.get(kind=Task.Kind.CREDENTIALS)
    assert credentials.status == Task.Status.OPEN


def test_cancelling_the_sign_in_task_keeps_the_job(person, fake_store, approver_user):
    from tests.fakes import FakeAdapter

    FakeAdapter.password = "Initial-Pass-123"
    fake_store.fail_group_with = TransientError("Fake app answered 503.")
    start_workflow(person, Workflow.Kind.ONBOARDING, actor=approver_user)
    run_all()
    job = Job.objects.get()
    assert job.status == Job.Status.QUEUED
    cancel_task(Task.objects.get(kind=Task.Kind.CREDENTIALS), actor=approver_user)
    job.refresh_from_db()
    assert job.status == Job.Status.QUEUED

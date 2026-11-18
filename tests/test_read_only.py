"""Read-only mode (READ_ONLY=1): the app reads connected apps, changes
nothing in them, sends no email, and rehearses every job."""

import copy
import os
import subprocess
import sys
from pathlib import Path

import pytest
from django.urls import reverse

from iam.adapters.base import ReadOnlyAdapter, ReadOnlyError
from iam.jobs import claim_next_job, notify_tasks, run_job, send_email
from iam.lifecycle import start_workflow
from iam.models import (
    Account,
    AppSettings,
    AuditEvent,
    Connection,
    Job,
    Person,
    Task,
    Workflow,
)
from iam.sync import compare_accounts, import_groups

pytestmark = pytest.mark.django_db

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def read_only(settings):
    settings.READ_ONLY = True


def run_all():
    while (job := claim_next_job()) is not None:
        run_job(job)


def add_account(store, *, active=True, groups=()):
    store.accounts["u-ada"] = {
        "username": "ada",
        "email": "ada@example.invalid",
        "active": active,
        "groups": set(groups),
    }
    return copy.deepcopy(store.accounts["u-ada"])


def test_onboarding_rehearses_and_changes_nothing(person, fake_store):
    person.team = None
    person.save()
    before = add_account(fake_store, active=False, groups={"g-eng", "unmanaged"})
    workflow = start_workflow(person, Workflow.Kind.ONBOARDING)
    run_all()
    job = workflow.jobs.get()
    assert job.status == Job.Status.REHEARSED
    assert job.result == {
        "account": "ada",
        "would": "reactivate the account",
        "groups_to_add": "Staff",
        "groups_to_remove": "Engineering",
    }
    assert fake_store.accounts["u-ada"] == before
    assert fake_store.calls == ["find"]
    workflow.refresh_from_db()
    person.refresh_from_db()
    assert workflow.status == Workflow.Status.COMPLETED
    assert person.status == Person.Status.ACTIVE
    assert not Task.objects.exists()
    assert not Account.objects.exists()
    assert AuditEvent.objects.filter(action="job.rehearsed", person=person).exists()


def test_a_missing_account_would_be_created(person, fake_store):
    start_workflow(person, Workflow.Kind.ONBOARDING)
    run_all()
    job = Job.objects.get()
    assert job.result == {
        "would": "create the account",
        "groups_to_add": "Engineering, Staff",
        "groups_to_remove": "none",
    }
    assert fake_store.accounts == {}


def test_offboarding_rehearses_the_removal(person, fake_store):
    person.status = Person.Status.ACTIVE
    person.save()
    before = add_account(fake_store, groups={"g-staff", "unmanaged"})
    workflow = start_workflow(person, Workflow.Kind.OFFBOARDING)
    run_all()
    job = workflow.jobs.get()
    assert job.status == Job.Status.REHEARSED
    assert job.result["would"] == "disable the account"
    assert job.result["groups_to_remove"] == "Staff"
    assert fake_store.accounts["u-ada"] == before
    person.refresh_from_db()
    assert person.status == Person.Status.OFFBOARDED


def test_connections_without_automation_open_no_task(person, fake_connection, fake_store):
    fake_connection.mode = Connection.Mode.MANUAL
    fake_connection.save()
    workflow = start_workflow(person, Workflow.Kind.ONBOARDING)
    assert workflow.jobs.get().status == Job.Status.QUEUED
    run_all()
    job = workflow.jobs.get()
    assert job.status == Job.Status.REHEARSED
    assert job.result["would"] == "ask a person to make the change"
    assert "Staff" in job.result["steps"]
    assert not Task.objects.exists()
    assert fake_store.calls == []


def test_someone_elses_account_still_stops_the_job(person, fake_store, fake_connection):
    other = Person.objects.create(first_name="Grace", last_name="Hopper")
    add_account(fake_store)
    Account.objects.create(
        connection=fake_connection, person=other, external_id="u-ada", username="ada"
    )
    start_workflow(person, Workflow.Kind.ONBOARDING)
    run_all()
    job = Job.objects.get()
    assert job.status == Job.Status.FAILED
    assert "belongs to Grace Hopper" in job.last_error
    assert fake_store.accounts["u-ada"]["active"]


def test_a_group_read_that_fails_fails_the_rehearsal(person, fake_store):
    from tests.fakes import AdapterError

    add_account(fake_store, groups={"g-staff"})
    fake_store.fail_group_read_with = AdapterError("Fake app answered 403.")
    workflow = start_workflow(person, Workflow.Kind.ONBOARDING)
    run_all()
    job = workflow.jobs.get()
    assert job.status == Job.Status.FAILED
    assert job.last_error == "Fake app answered 403."
    workflow.refresh_from_db()
    assert workflow.status == Workflow.Status.FAILED


def test_adapters_can_only_read(fake_connection, fake_store):
    adapter = fake_connection.build_adapter()
    assert isinstance(adapter, ReadOnlyAdapter)
    assert adapter.test_connection().ok
    assert adapter.list_accounts() == []
    assert adapter.supports_groups
    assert getattr(adapter, "can_read_accounts", None) is None
    for name in (
        "ensure_account",
        "disable_account",
        "create_account",
        "set_active",
        "add_to_group",
        "remove_from_group",
        "send",
        "an_operation_added_later",
    ):
        with pytest.raises(ReadOnlyError, match="Read-only mode"):
            getattr(adapter, name)


def test_reads_from_connections_still_work(person, fake_connection, fake_store):
    add_account(fake_store, groups={"g-staff"})
    assert import_groups(fake_connection).status == "succeeded"
    run = compare_accounts(fake_connection)
    assert run.status == "succeeded", run.error
    assert Account.objects.get(external_id="u-ada").person == person


def test_no_email_leaves_the_app(db, monkeypatch):
    from iam.adapters.smtp import SmtpAdapter

    sent = []
    monkeypatch.setattr(SmtpAdapter, "send", lambda self, **kwargs: sent.append(kwargs))
    Connection.objects.create(adapter="smtp", name="Mail", enabled=True, settings={})
    settings = AppSettings.load()
    settings.notification_emails = "it@example.invalid"
    settings.save()
    assert send_email(["ada@example.invalid"], "Hello", "Body") is False
    notify_tasks([Task(title="Something")])
    assert sent == []


def test_every_page_says_so(client_for, viewer_user, settings):
    client = client_for(viewer_user)
    assert "Read-only mode." in client.get(reverse("dashboard")).content.decode()
    settings.READ_ONLY = False
    assert "Read-only mode." not in client.get(reverse("dashboard")).content.decode()


def test_the_workflow_page_shows_what_each_job_would_do(
    client_for, viewer_user, person, fake_store
):
    workflow = start_workflow(person, Workflow.Kind.ONBOARDING)
    run_all()
    page = client_for(viewer_user).get(reverse("workflow_detail", args=[workflow.pk]))
    assert "Would create the account · add to Engineering, Staff" in page.content.decode()


def test_the_health_check_reports_the_mode(client, settings):
    settings.HEALTHCHECK_TOKEN = "health-token"
    response = client.get("/-/readyz", headers={"X-Health-Token": "health-token"})
    assert response.json()["read_only"] is True


def read_only_setting(value: str) -> subprocess.CompletedProcess:
    env = {
        "PATH": os.environ.get("PATH", ""),
        "DJANGO_DEBUG": "1",
        "DEPLOYMENT_ENV": "development",
        "READ_ONLY": value,
    }
    return subprocess.run(
        [sys.executable, "-c", "import config.settings as s; print(s.READ_ONLY)"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
    )


def test_the_setting_accepts_only_a_clear_yes_or_no():
    assert read_only_setting("1").stdout.strip() == "True"
    assert read_only_setting("true").stdout.strip() == "True"
    assert read_only_setting("").stdout.strip() == "False"
    assert read_only_setting("0").stdout.strip() == "False"
    typo = read_only_setting("ture")
    assert typo.returncode != 0 and "READ_ONLY must be 1 or 0" in typo.stderr

from datetime import date, timedelta

import pytest
from django.core.management import call_command
from django.utils import timezone

from iam.adapters.base import HRRecord
from iam.models import (
    AccessProfile,
    Account,
    AppSettings,
    Connection,
    Job,
    Person,
    ProviderGroup,
    SyncRun,
    Team,
    WorkerHeartbeat,
    Workflow,
)
from iam.schedule import run_due_syncs, start_due_offboarding, start_due_onboarding
from iam.sync import compare_accounts, import_groups, import_people

pytestmark = pytest.mark.django_db


def test_group_import_creates_updates_and_marks_missing(fake_connection, fake_store):
    stale = ProviderGroup.objects.create(
        connection=fake_connection, external_id="gone", name="Gone"
    )
    run = import_groups(fake_connection)
    assert run.status == SyncRun.Status.SUCCEEDED
    assert run.summary == {"groups": 3, "created": 3, "updated": 0, "missing": 1}
    stale.refresh_from_db()
    assert stale.missing
    assert ProviderGroup.objects.get(external_id="g-eng").path == "/Engineering"


def test_failed_reads_are_recorded_not_raised(fake_connection, fake_store):
    from iam.adapters.base import ConfigurationError

    fake_store.fail_with = ConfigurationError("Token refused.")
    run = compare_accounts(fake_connection)
    assert run.status == SyncRun.Status.FAILED
    assert run.error == "Token refused."


def test_account_comparison_links_by_email_and_reports_drift(fake_connection, fake_store, person):
    fake_store.accounts["x1"] = {
        "username": "ada",
        "email": "ADA@example.invalid",
        "active": True,
        "groups": set(),
    }
    fake_store.accounts["x2"] = {
        "username": "stranger",
        "email": "stranger@example.invalid",
        "active": True,
        "groups": set(),
    }
    gone = Account.objects.create(
        connection=fake_connection, external_id="old", status=Account.Status.ACTIVE
    )
    Person.objects.filter(pk=person.pk).update(status=Person.Status.OFFBOARDED)
    run = compare_accounts(fake_connection)
    assert run.status == SyncRun.Status.SUCCEEDED
    assert Account.objects.get(external_id="x1").person_id == person.pk
    assert Account.objects.get(external_id="x2").person is None
    gone.refresh_from_db()
    assert gone.status == Account.Status.MISSING
    assert run.summary["new_links"] == 1
    assert run.summary["unlinked_active"] == 1
    assert run.summary["unexpected_access"] == [
        {"id": person.pk, "name": "Ada Lovelace", "status": "offboarded"}
    ]


def test_account_comparison_reports_missing_access(fake_connection, fake_store, person):
    Person.objects.filter(pk=person.pk).update(status=Person.Status.ACTIVE)
    run = compare_accounts(fake_connection)
    assert run.summary["missing_access"] == [{"id": person.pk, "name": "Ada Lovelace"}]


class FakeHR:
    records: list[HRRecord] = []


@pytest.fixture
def hr_connection(db, monkeypatch):
    from iam import adapters
    from iam.adapters.base import Adapter, AdapterKind

    class HRAdapter(Adapter):
        slug = "fake_hr"
        name = "Fake HR"
        kind = AdapterKind.HR_SOURCE
        supports_write = False

        def list_people(self):
            return list(FakeHR.records)

    import sys
    import types

    module = types.ModuleType("tests.fake_hr")
    module.HRAdapter = HRAdapter
    monkeypatch.setitem(sys.modules, "tests.fake_hr", module)
    monkeypatch.setitem(adapters._ADAPTERS, "fake_hr", ("tests.fake_hr", "HRAdapter"))
    return Connection.objects.create(
        adapter="fake_hr", name="HR", enabled=True, settings={"sync_interval_minutes": 60}
    )


def test_people_import_creates_and_updates_people(hr_connection):
    staff = AccessProfile.objects.create(name="Staff", default_for=["employee"])
    team = Team.objects.create(name="Product")
    manager = Person.objects.create(
        first_name="Grace", last_name="Hopper", work_email="grace@example.invalid"
    )
    start = date.today() + timedelta(days=10)
    FakeHR.records = [
        HRRecord(
            external_id="42",
            first_name="Ada",
            last_name="Lovelace",
            work_email="Ada@example.invalid",
            person_type="employee",
            team="product",
            manager_email="grace@example.invalid",
            start_date=start,
        ),
        HRRecord(external_id="43", first_name="Left", last_name="Long Ago", active=False),
    ]
    run = import_people(hr_connection)
    assert run.status == SyncRun.Status.SUCCEEDED, run.error
    ada = Person.objects.get(source_id="42")
    assert (ada.work_email, ada.team, ada.manager, ada.access_profile, ada.start_date) == (
        "ada@example.invalid",
        team,
        manager,
        staff,
        start,
    )
    assert ada.status == Person.Status.PLANNED
    assert not Person.objects.filter(source_id="43").exists()
    FakeHR.records[0].end_date = start + timedelta(days=90)
    FakeHR.records[0].work_email = "ada.new@example.invalid"
    run = import_people(hr_connection)
    ada.refresh_from_db()
    assert ada.end_date == start + timedelta(days=90)
    assert ada.work_email == "ada@example.invalid", "a work email never changes by import"
    assert run.summary["work_email_differs"][0]["id"] == ada.pk
    assert run.summary["updated"][0]["fields"] == ["end_date"]


def test_people_import_claims_existing_people_by_work_email(hr_connection):
    existing = Person.objects.create(
        first_name="Ada", last_name="L", work_email="ada@example.invalid"
    )
    FakeHR.records = [
        HRRecord(
            external_id="7",
            first_name="Ada",
            last_name="Lovelace",
            work_email="ada@example.invalid",
        )
    ]
    import_people(hr_connection)
    existing.refresh_from_db()
    assert (existing.source_connection, existing.source_id, existing.last_name) == (
        hr_connection,
        "7",
        "Lovelace",
    )


def test_people_missing_from_hr_are_reported_not_offboarded(hr_connection):
    person = Person.objects.create(
        first_name="Ada",
        last_name="Lovelace",
        status=Person.Status.ACTIVE,
        source_connection=hr_connection,
        source_id="9",
    )
    FakeHR.records = []
    run = import_people(hr_connection)
    person.refresh_from_db()
    assert person.status == Person.Status.ACTIVE
    assert run.summary["not_listed"] == [{"id": person.pk, "name": "Ada Lovelace"}]


def test_schedule_starts_onboarding_within_the_lead_time(person):
    person.start_date = timezone.localdate() + timedelta(days=3)
    person.save()
    later = Person.objects.create(
        first_name="Later",
        last_name="Person",
        access_profile=person.access_profile,
        start_date=timezone.localdate() + timedelta(days=30),
    )
    started = start_due_onboarding()
    assert [w.person_id for w in started] == [person.pk]
    assert started[0].automatic
    later.refresh_from_db()
    assert later.status == Person.Status.PLANNED


def test_schedule_respects_the_automatic_onboarding_switch(person):
    AppSettings.objects.update_or_create(pk=1, defaults={"automatic_onboarding": False})
    person.start_date = timezone.localdate()
    person.save()
    assert start_due_onboarding() == []


def test_schedule_offboards_the_day_after_the_last_day(person):
    Person.objects.filter(pk=person.pk).update(
        status=Person.Status.ACTIVE, end_date=timezone.localdate()
    )
    assert start_due_offboarding() == []
    Person.objects.filter(pk=person.pk).update(end_date=timezone.localdate() - timedelta(days=1))
    started = start_due_offboarding()
    assert len(started) == 1 and started[0].kind == Workflow.Kind.OFFBOARDING


def test_due_syncs_run_on_their_interval(fake_connection, fake_store):
    fake_connection.settings = {"sync_interval_minutes": 30}
    fake_connection.save()
    assert run_due_syncs() == 1
    assert run_due_syncs() == 0
    Connection.objects.filter(pk=fake_connection.pk).update(
        last_sync_at=timezone.now() - timedelta(minutes=31)
    )
    assert run_due_syncs() == 1


@pytest.mark.django_db(transaction=True)
def test_worker_runs_jobs_and_beats(person, fake_store):
    # The worker closes stale connections each round, so it needs a real transaction.
    from iam.lifecycle import start_workflow

    start_workflow(person, Workflow.Kind.ONBOARDING)
    call_command("run_worker", "--once")
    assert Job.objects.get().status == Job.Status.SUCCEEDED
    assert WorkerHeartbeat.objects.filter(name="worker").exists()


def test_one_failing_person_does_not_stop_the_schedule(person, monkeypatch):
    import iam.schedule

    other = Person.objects.create(
        first_name="Grace",
        last_name="Hopper",
        work_email="grace@example.invalid",
        access_profile=person.access_profile,
        start_date=timezone.localdate(),
    )
    Person.objects.filter(pk=person.pk).update(start_date=timezone.localdate())
    original = iam.schedule.start_workflow

    def fail_for_ada(p, *args, **kwargs):
        if p.pk == person.pk:
            raise RuntimeError("database hiccup")
        return original(p, *args, **kwargs)

    monkeypatch.setattr(iam.schedule, "start_workflow", fail_for_ada)
    started = start_due_onboarding()
    assert [w.person_id for w in started] == [other.pk]


@pytest.mark.django_db(transaction=True)
def test_a_failing_schedule_does_not_stop_the_jobs(person, fake_store, monkeypatch):
    from iam.lifecycle import start_workflow

    def broken():
        raise RuntimeError("schedule broke")

    monkeypatch.setattr("iam.management.commands.run_worker.run_schedule", broken)
    start_workflow(person, Workflow.Kind.ONBOARDING)
    call_command("run_worker", "--once")
    assert Job.objects.get().status == Job.Status.SUCCEEDED


@pytest.mark.django_db(transaction=True)
def test_one_worker_at_a_time_runs_the_schedule(person):
    from django.db import connection, connections

    from iam.schedule import SCHEDULE_LOCK_ID, run_schedule

    if connection.vendor != "postgresql":
        pytest.skip("SQLite runs one worker.")
    Person.objects.filter(pk=person.pk).update(start_date=timezone.localdate())
    other = connections.create_connection("default")
    try:
        with other.cursor() as cursor:
            cursor.execute("SELECT pg_advisory_lock(%s)", [SCHEDULE_LOCK_ID])
        assert run_schedule() == {}
        person.refresh_from_db()
        assert person.status == Person.Status.PLANNED
    finally:
        other.close()
    assert run_schedule()["onboarding"] == 1


def test_drift_reports_planned_people_with_active_accounts(fake_connection, fake_store, person):
    fake_store.accounts["x1"] = {
        "username": "ada",
        "email": "ada@example.invalid",
        "active": True,
        "groups": set(),
    }
    run = compare_accounts(fake_connection)
    assert run.summary["unexpected_access"] == [
        {"id": person.pk, "name": "Ada Lovelace", "status": "planned"}
    ]


def test_schedule_offboards_a_planned_person_with_an_app_user(person):
    from iam.models import User

    user = User.objects.create_user(email="ada@example.invalid", person=person)
    Person.objects.filter(pk=person.pk).update(end_date=timezone.localdate() - timedelta(days=1))
    assert len(start_due_offboarding()) == 1
    user.refresh_from_db()
    assert not user.is_active


def test_a_new_start_date_from_the_hr_import_lifts_a_paused_onboarding(fake_store, person):
    from iam.sync import _import_record

    Person.objects.filter(pk=person.pk).update(onboarding_paused=True, source_id="p1")
    person.refresh_from_db()
    connection = Connection.objects.create(adapter="personio", name="Personio")
    Person.objects.filter(pk=person.pk).update(source_connection=connection)
    record = HRRecord(
        external_id="p1",
        first_name="Ada",
        last_name="Lovelace",
        work_email="ada@example.invalid",
        start_date=date(2027, 1, 4),
    )
    _import_record(
        connection,
        record,
        {"work_email_differs": [], "updated": [], "inactive_without_end_date": []},
        None,
    )
    person.refresh_from_db()
    assert not person.onboarding_paused and person.start_date == date(2027, 1, 4)


def test_an_account_listed_twice_is_recorded_once(fake_connection):
    from iam.accounts import record_listing
    from iam.adapters.base import ExternalAccount

    twice = ExternalAccount(external_id="x1", username="ada", email="ada@example.invalid")
    summary = record_listing(fake_connection, [twice, twice])
    assert summary["accounts"] == 1
    assert Account.objects.filter(external_id="x1").count() == 1


def change_meanwhile(change):
    """Run `change` in another connection that holds its row locks for half a
    second, as a job or workflow committing at the same time would."""
    import threading
    import time

    from django.db import connections, transaction

    started = threading.Event()

    def run():
        try:
            with transaction.atomic():
                change()
                started.set()
                time.sleep(0.5)
        finally:
            connections.close_all()

    thread = threading.Thread(target=run)
    thread.start()
    assert started.wait(5)
    return thread


def needs_postgresql():
    from django.db import connection

    if connection.vendor != "postgresql":
        pytest.skip("Only PostgreSQL runs transactions side by side.")


@pytest.mark.django_db(transaction=True)
def test_a_comparison_keeps_the_groups_a_job_records_meanwhile(fake_connection, person):
    from iam.accounts import record_listing
    from iam.adapters.base import ExternalAccount

    needs_postgresql()
    account = Account.objects.create(
        connection=fake_connection, external_id="x1", person=person, applied_groups=["g-staff"]
    )
    thread = change_meanwhile(
        lambda: Account.objects.filter(pk=account.pk).update(applied_groups=["g-eng", "g-staff"])
    )
    record_listing(fake_connection, [ExternalAccount(external_id="x1", username="ada")])
    thread.join()
    account.refresh_from_db()
    assert account.applied_groups == ["g-eng", "g-staff"]


@pytest.mark.django_db(transaction=True)
def test_the_hr_import_keeps_a_status_a_workflow_sets_meanwhile(person):
    from django.db import transaction

    from iam.sync import _import_record

    needs_postgresql()
    connection = Connection.objects.create(adapter="personio", name="Personio")
    Person.objects.filter(pk=person.pk).update(
        status=Person.Status.ACTIVE, source_connection=connection, source_id="p1"
    )
    thread = change_meanwhile(
        lambda: Person.objects.filter(pk=person.pk).update(status=Person.Status.OFFBOARDING)
    )
    record = HRRecord(external_id="p1", first_name="Ada", last_name="King")
    summary = {"work_email_differs": [], "updated": [], "inactive_without_end_date": []}
    with transaction.atomic():  # as import_people runs each record
        _import_record(connection, record, summary, None)
    thread.join()
    person.refresh_from_db()
    assert person.last_name == "King" and person.status == Person.Status.OFFBOARDING

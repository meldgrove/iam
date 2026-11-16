import pytest

from iam.adapters.base import EnsureResult, ExternalAccount
from iam.jobs import complete_task
from iam.lifecycle import start_workflow
from iam.models import Account, Connection, Task, Workflow
from iam.sync import compare_accounts

pytestmark = pytest.mark.django_db


def listed(**values):
    return {"active": True, "groups": set(), **values}


def test_a_comparison_keeps_the_groups_a_manual_change_applied(
    person, fake_connection, fake_store, approver_user
):
    fake_connection.mode = Connection.Mode.MANUAL
    fake_connection.save()
    workflow = start_workflow(person, Workflow.Kind.ONBOARDING)
    complete_task(Task.objects.get(job__workflow=workflow), actor=approver_user)
    assert Account.objects.get(person=person).applied_groups == ["g-eng", "g-staff"]
    # Someone created the account by hand; the comparison finds it.
    fake_store.accounts["x1"] = listed(username="ada", email="ada@example.invalid")
    compare_accounts(fake_connection)
    account = Account.objects.get(person=person)
    assert account.external_id == "x1"
    assert account.applied_groups == ["g-eng", "g-staff"]


def test_a_comparison_keeps_values_the_app_does_not_report(person, fake_connection, fake_store):
    fake_store.accounts["x1"] = listed(username="ada", email="ada@example.invalid")
    compare_accounts(fake_connection)
    fake_store.accounts["x1"]["email"] = ""
    compare_accounts(fake_connection)
    account = Account.objects.get(external_id="x1")
    assert account.person == person and account.email == "ada@example.invalid"


def test_job_results_keep_values_within_their_columns(person, fake_connection):
    from iam.accounts import record_job_result

    job = start_workflow(person, Workflow.Kind.ONBOARDING).jobs.get()
    long = "a" * 300
    external = ExternalAccount("x1", username=long, email=long, display_name=long)
    record_job_result(job, EnsureResult(account=external, created=True))
    account = Account.objects.get(person=person)
    assert {len(account.username), len(account.email), len(account.display_name)} == {255}

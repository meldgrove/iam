"""How a claimed job ends: every outcome of its attempt at the connection,
in every situation the job can be in when finish records it.

A worker claims a job, makes the change at the connection (attempt), and
records the outcome (finish). Meanwhile a newer workflow may replace the
job's workflow, another worker may claim the job after this claim's lease
ran out, or someone may close the job. Each row pins what the app records
then: the job's and the workflow's status, the open tasks, the audit
events, the person's account, and whether the sign-in details reached the
person.
"""

from datetime import timedelta

import pytest
from django.db import transaction
from django.utils import timezone

from iam.adapters.base import AdapterError, TransientError
from iam.jobs import (
    attempt,
    cancel_job,
    claim_next_job,
    finish,
    recover_stale_jobs,
    refresh_workflow,
)
from iam.lifecycle import start_workflow
from iam.models import Account, AuditEvent, Connection, Job, Person, Task, Workflow

pytestmark = pytest.mark.django_db

SITUATIONS = ("current", "replaced", "claimed elsewhere", "closed meanwhile")

SIGNED_IN = ["credentials.sent", "job.ensure_account"]
NOTHING = []

# outcome: one row per situation, in the order of SITUATIONS:
# (job status, workflow status, open tasks, job audit events, account, emails)
EXPECTED = {
    # The account was created with its groups.
    "changed": [
        ("succeeded", "completed", [], SIGNED_IN, "active", 1),
        ("succeeded", "cancelled", [], SIGNED_IN, "active", 1),
        ("running", "running", [], SIGNED_IN, "active", 1),
        # A success counts even for a job closed while it ran (a question
        # for later: keep it cancelled and only record the account?).
        ("succeeded", "completed", [], SIGNED_IN, "active", 1),
    ],
    # The account was created; a group step failed for good.
    "groups failed": [
        ("succeeded", "completed", ["failure"], SIGNED_IN, "active", 1),
        ("succeeded", "cancelled", [], SIGNED_IN, "active", 1),
        ("running", "running", [], SIGNED_IN, "active", 1),
        ("succeeded", "completed", ["failure"], SIGNED_IN, "active", 1),
    ],
    # The account was created; a group step failed for now, before the last attempt.
    "groups pending": [
        ("queued", "running", [], SIGNED_IN, "active", 1),
        ("cancelled", "cancelled", [], SIGNED_IN, "active", 1),
        ("running", "running", [], SIGNED_IN, "active", 1),
        ("cancelled", "completed", [], SIGNED_IN, "active", 1),
    ],
    # The account changed, but by then it was linked to someone else.
    "linked to someone else": [
        ("failed", "failed", ["failure"], ["job.failed"], None, 0),
        ("cancelled", "cancelled", [], NOTHING, None, 0),
        ("running", "running", [], NOTHING, None, 0),
        ("cancelled", "completed", [], NOTHING, None, 0),
    ],
    "failed for now": [
        ("queued", "running", [], NOTHING, None, 0),
        ("cancelled", "cancelled", [], NOTHING, None, 0),
        ("running", "running", [], NOTHING, None, 0),
        ("cancelled", "completed", [], NOTHING, None, 0),
    ],
    "failed": [
        ("failed", "failed", ["failure"], ["job.failed"], None, 0),
        ("cancelled", "cancelled", [], NOTHING, None, 0),
        ("running", "running", [], NOTHING, None, 0),
        ("cancelled", "completed", [], NOTHING, None, 0),
    ],
    # The connection stopped changing accounts automatically meanwhile.
    "for a person": [
        ("manual", "waiting", ["change"], NOTHING, None, 0),
        ("cancelled", "cancelled", [], NOTHING, None, 0),
        ("running", "running", [], NOTHING, None, 0),
        ("cancelled", "completed", [], NOTHING, None, 0),
    ],
    "rehearsed": [
        ("rehearsed", "completed", [], ["job.rehearsed"], None, 0),
        ("cancelled", "cancelled", [], NOTHING, None, 0),
        ("running", "running", [], NOTHING, None, 0),
        ("cancelled", "completed", [], NOTHING, None, 0),
    ],
}


def arrange_outcome(outcome, store, connection, settings):
    """Arrange the attempt's outcome. Returns what happens between the
    attempt and finish, if anything."""
    if outcome == "groups failed":
        store.fail_group_with = AdapterError("Fake app answered 400.")
    elif outcome == "groups pending":
        store.fail_group_with = TransientError("Fake app answered 503.")
    elif outcome == "linked to someone else":
        store.accounts["u-ada"] = {
            "username": "ada",
            "email": "ada@example.invalid",
            "active": True,
            "groups": set(),
        }

        def link_to_someone_else():
            # After the attempt checked whose account it changes.
            grace = Person.objects.create(first_name="Grace", last_name="Hopper")
            Account.objects.create(
                connection=connection, person=grace, external_id="u-ada", username="ada"
            )

        return link_to_someone_else
    elif outcome == "failed for now":
        store.fail_with = TransientError("Fake app answered 503.")
    elif outcome == "failed":
        store.fail_with = AdapterError("Fake app answered 400.")
    elif outcome == "for a person":
        Connection.objects.filter(pk=connection.pk).update(mode=Connection.Mode.MANUAL)
    elif outcome == "rehearsed":
        settings.READ_ONLY = True
    return None


def arrange_situation(situation, claim, person):
    if situation == "replaced":
        # Applying access during onboarding starts a new onboarding.
        start_workflow(person, Workflow.Kind.ACCESS_UPDATE)
    elif situation == "claimed elsewhere":
        Job.objects.filter(pk=claim.pk).update(locked_until=timezone.now() - timedelta(seconds=1))
        assert recover_stale_jobs() == 1
        assert claim_next_job().pk == claim.pk
    elif situation == "closed meanwhile":
        with transaction.atomic():
            cancel_job(Job.objects.get(pk=claim.pk), note="Closed meanwhile.")
            refresh_workflow(claim.workflow)


def observe(job, person, sent):
    job.refresh_from_db()
    return (
        job.status,
        Workflow.objects.get(pk=job.workflow_id).status,
        sorted(
            Task.objects.filter(job=job, status=Task.Status.OPEN).values_list("kind", flat=True)
        ),
        sorted(
            AuditEvent.objects.filter(target_type="job", target_id=str(job.pk)).values_list(
                "action", flat=True
            )
        ),
        Account.objects.filter(person=person).values_list("status", flat=True).first(),
        len(sent),
    )


CASES = [
    pytest.param(outcome, situation, row, id=f"{outcome}, {situation}")
    for outcome, rows in EXPECTED.items()
    for situation, row in zip(SITUATIONS, rows, strict=True)
]


@pytest.mark.parametrize(("outcome", "situation", "expected"), CASES)
def test_how_a_claimed_job_ends(
    outcome, situation, expected, person, fake_store, fake_connection, settings, monkeypatch
):
    from tests.fakes import FakeAdapter

    FakeAdapter.password = "Initial-Pass-123"
    sent = []
    monkeypatch.setattr("iam.jobs.send_email", lambda to, subject, body: sent.append(to) or True)
    start_workflow(person, Workflow.Kind.ONBOARDING)
    claim = claim_next_job()
    meanwhile = arrange_outcome(outcome, fake_store, fake_connection, settings)
    attempted = attempt(claim)
    if meanwhile is not None:
        meanwhile()
    arrange_situation(situation, claim, person)
    finish(claim, attempted)
    assert observe(claim, person, sent) == expected


@pytest.mark.django_db(transaction=True)
def test_sign_in_details_go_out_after_a_group_retry_commits(person, fake_store, monkeypatch):
    """When a group step fails for now, the account and the requeue commit
    together, and only then does the initial password leave the app."""
    from django.db import connection

    from tests.fakes import FakeAdapter

    FakeAdapter.password = "Initial-Pass-123"
    fake_store.fail_group_with = TransientError("Fake app answered 503.")
    seen = []

    def send_email(to, subject, body):
        recorded = Account.objects.filter(person=person).exists()
        seen.append((connection.in_atomic_block, Job.objects.get().status, recorded))
        return True

    monkeypatch.setattr("iam.jobs.send_email", send_email)
    start_workflow(person, Workflow.Kind.ONBOARDING)
    claim = claim_next_job()
    finish(claim, attempt(claim))
    assert seen == [(False, Job.Status.QUEUED, True)]

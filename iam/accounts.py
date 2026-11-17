"""Accounts: what this app knows about an account in a connected app.

The only module that writes Account rows. Jobs record what a change did,
completed manual tasks record what a person did by hand, and account
comparisons record what the app listed. The same rules apply to all three:

- An account linked to one person is never linked to, or changed for,
  another person.
- A row keeps what the app reported, each value up to 255 characters, and
  its old value where the app reports none.
- The applied-groups ledger lists the groups this app put the account in.
  They stay removable after a profile or team stops using them. A row
  without an ID stands for the same account (a manual change recorded it),
  so its ledger carries over to the listed account that replaces it.
"""

from __future__ import annotations

from collections.abc import Iterable

from django.db import IntegrityError, transaction
from django.db.models import Q
from django.utils import timezone

from .adapters.base import DisableResult, EnsureResult, ExternalAccount
from .models import Account, Connection, Job, Person, ProviderGroup


class AccountConflict(Exception):
    """The account is linked to someone else; the message says to whom."""


def other_owner(connection: Connection, external_id: str, person: Person) -> Person | None:
    """The person other than this one that the app's account is linked to."""
    account = (
        Account.objects.filter(connection=connection, external_id=external_id)
        .exclude(person=None)
        .exclude(person=person)
        .select_related("person")
        .first()
    )
    return account.person if account else None


def removable_groups(connection_id: int, person_id: int, managed: Iterable[str]) -> set[str]:
    """The groups a change may remove from the person's account: the managed
    groups, plus the groups this app has put the account in. Jobs for the
    same person and app run one after another, so this includes what a job
    that was still running at planning time applied. Groups marked missing
    are left alone: an import that read nothing marks every group missing."""
    account = Account.objects.filter(connection_id=connection_id, person_id=person_id).first()
    applied = set(account.applied_groups or []) if account else set()
    return (set(managed) | applied) - missing_groups(connection_id)


def missing_groups(connection_id: int) -> set[str]:
    """Groups the app no longer lists. Their memberships stay as they are
    until an import sees them again."""
    return set(
        ProviderGroup.objects.filter(connection_id=connection_id, missing=True).values_list(
            "external_id", flat=True
        )
    )


def record_job_result(job: Job, result: EnsureResult | DisableResult) -> None:
    """Record what a job did to the person's account. Raises AccountConflict
    when the app's account is linked to someone else."""
    external = result.account
    existing = Account.objects.filter(connection=job.connection, person=job.person).first()
    if external is None:
        if existing is not None:
            existing.status = Account.Status.MISSING
            existing.applied_groups = []
            existing.observed_at = timezone.now()
            existing.save()
        return
    if external.external_id:
        other = (
            Account.objects.filter(connection=job.connection, external_id=external.external_id)
            .exclude(pk=existing.pk if existing else None)
            .first()
        )
        if other is not None:
            if other.person_id and other.person_id != job.person_id:
                raise AccountConflict(
                    f"The {job.connection.name} account {external.username or external.external_id} "
                    f"is linked to {other.person}. Check the work email and username."
                )
            if existing is not None:
                _replace(existing, other)
            existing = other
    account = existing or Account(connection=job.connection)
    account.person = job.person
    _observe(account, external)
    if isinstance(result, EnsureResult):
        account.created_by_app = account.created_by_app or result.created
        if result.group_error is None:
            # Missing groups were left alone, so they stay in the ledger.
            kept = set(account.applied_groups or []) & missing_groups(job.connection_id)
            account.applied_groups = sorted(set(job.groups) | kept)
        else:
            # Unfinished: keep every group the app may have to remove later.
            applied = set(account.applied_groups or []) | set(job.groups)
            account.applied_groups = sorted(applied - set(result.groups_removed))
    else:
        removed = set(result.groups_removed)
        account.applied_groups = [g for g in account.applied_groups or [] if g not in removed]
    try:
        with transaction.atomic():
            account.save()
    except IntegrityError as exc:
        raise AccountConflict(
            f"The {job.connection.name} account could not be linked to {job.person}."
        ) from exc


def record_manual_change(job: Job) -> None:
    """Record a change a person made by hand. Without the app's ID, the row
    stands for the account until a comparison finds it."""
    account, _ = Account.objects.get_or_create(connection=job.connection, person=job.person)
    account.status = (
        Account.Status.DISABLED
        if job.action == Job.Action.DISABLE_ACCOUNT
        else Account.Status.ACTIVE
    )
    account.applied_groups = sorted(job.groups) if job.action == Job.Action.ENSURE_ACCOUNT else []
    if job.action == Job.Action.ENSURE_ACCOUNT:
        account.observed_groups = sorted(job.groups)
    account.observed_at = timezone.now()
    account.save()


def record_listing(connection: Connection, listed: Iterable[ExternalAccount]) -> dict:
    """Record an account comparison: update the accounts the app lists, link
    an unlinked one to a person on a unique match, and mark the accounts it
    no longer lists as missing."""
    now = timezone.now()
    seen_ids = set()
    new_links = 0
    with transaction.atomic():
        # Locked: the rows are saved whole, and a job may record its account
        # (and the groups it applied) meanwhile.
        by_id = {
            a.external_id: a
            # Only the account rows: the ordering joins the connection.
            for a in connection.accounts.exclude(external_id="").select_for_update(of=("self",))
        }
        for external in listed:
            # A listing paged by offset can list an account twice.
            if not external.external_id or external.external_id in seen_ids:
                continue
            seen_ids.add(external.external_id)
            account = by_id.get(external.external_id) or Account(
                connection=connection, external_id=external.external_id
            )
            _observe(account, external, now)
            if account.person_id is None:
                person = _match_person(connection, external)
                if person is not None:
                    placeholder = Account.objects.filter(
                        connection=connection, person=person, external_id=""
                    ).first()
                    if placeholder is not None:
                        _replace(placeholder, account)
                    account.person = person
                    new_links += 1
            account.save()
        missing = (
            connection.accounts.exclude(external_id="")
            .exclude(external_id__in=seen_ids)
            .exclude(status=Account.Status.MISSING)
            .update(status=Account.Status.MISSING, observed_at=now)
        )
    return {"accounts": len(seen_ids), "new_links": new_links, "missing": missing}


def _observe(account: Account, external: ExternalAccount, now=None) -> None:
    account.external_id = external.external_id or account.external_id
    account.username = (external.username or account.username)[:255]
    account.email = (external.email or account.email)[:255]
    account.display_name = (external.display_name or account.display_name)[:255]
    account.status = Account.Status.ACTIVE if external.active else Account.Status.DISABLED
    if external.groups is not None:
        account.observed_groups = sorted(external.groups)
    account.observed_at = now or timezone.now()


def _replace(old: Account, new: Account) -> None:
    """Drop the person's old row for the account that replaces it. A row
    without an ID stands for the same account, so its ledger carries over."""
    if not old.external_id:
        new.applied_groups = sorted(set(new.applied_groups or []) | set(old.applied_groups or []))
        new.created_by_app = new.created_by_app or old.created_by_app
    old.delete()


def _match_person(connection: Connection, external: ExternalAccount) -> Person | None:
    filters = Q()
    if external.email:
        filters |= Q(work_email__iexact=external.email)
    if external.username:
        filters |= Q(username__iexact=external.username)
        if "@" in external.username:
            filters |= Q(work_email__iexact=external.username)
    if not filters:
        return None
    candidates = list(Person.objects.filter(filters)[:2])
    if len(candidates) != 1:
        return None
    person = candidates[0]
    linked = Account.objects.filter(connection=connection, person=person).exclude(external_id="")
    return None if linked.exists() else person

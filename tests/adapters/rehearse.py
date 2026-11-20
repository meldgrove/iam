"""Rehearse a job at an adapter as read-only mode does."""

from iam.adapters.base import AccountSpec, Adapter, ReadOnlyAdapter


def rehearse(
    adapter: Adapter,
    action: str,
    spec: AccountSpec,
    *,
    external_id: str = "",
    groups=(),
    managed_groups=(),
):
    """What the job would do (an AccountPlan), read through the read-only
    wrapper: a step that changes anything raises ReadOnlyError."""
    reader = ReadOnlyAdapter(adapter)
    found = reader.find_account(spec, external_id)
    return reader.plan(
        action, spec, found=found, groups=set(groups), managed_groups=set(managed_groups)
    )

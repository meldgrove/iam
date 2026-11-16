"""Desired access: which accounts and groups a person should have.

Access comes from two places. A person's access profile grants an account at
each of its connections, with groups. A person's team adds its groups, but
only at connections where the profile grants an account. People without
access (planned, offboarding, offboarded, archived) should have nothing.

Managed groups are all groups any profile or team references. The app adds
and removes only managed groups; other memberships in a connected app are
left alone.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .adapters import AdapterKind
from .models import Connection, Person, ProfileGrant, ProviderGroup, Team


@dataclass
class Grant:
    connection: Connection
    groups: dict[str, ProviderGroup] = field(default_factory=dict)
    # Why each group applies: "profile" or "team".
    reasons: dict[str, set[str]] = field(default_factory=dict)

    def add(self, group: ProviderGroup, reason: str) -> None:
        self.groups[group.external_id] = group
        self.reasons.setdefault(group.external_id, set()).add(reason)

    @property
    def group_ids(self) -> list[str]:
        return sorted(self.groups)


def provisioning_connections(*, include_disabled: bool = False):
    connections = Connection.objects.all()
    if not include_disabled:
        connections = connections.filter(enabled=True)
    return [
        c for c in connections if c.adapter_class is not None and c.kind == AdapterKind.PROVISIONING
    ]


def entitled_access(person: Person, *, include_disabled: bool = False) -> dict[int, Grant]:
    """What the person's profile and team grant, regardless of status.
    Turned-off connections count only when asked for (offboarding)."""
    grants: dict[int, Grant] = {}
    if person.access_profile_id is None:
        return grants
    profile_grants = ProfileGrant.objects.filter(profile_id=person.access_profile_id)
    if not include_disabled:
        profile_grants = profile_grants.filter(connection__enabled=True)
    profile_grants = profile_grants.select_related("connection").prefetch_related("groups")
    for profile_grant in profile_grants:
        if profile_grant.connection.kind != AdapterKind.PROVISIONING:
            continue
        grant = Grant(connection=profile_grant.connection)
        for group in profile_grant.groups.all():
            if not group.missing:
                grant.add(group, "profile")
        grants[profile_grant.connection_id] = grant
    if person.team_id:
        for group in person.team.groups.filter(missing=False):
            if group.connection_id in grants:
                grants[group.connection_id].add(group, "team")
    return grants


def desired_access(person: Person) -> dict[int, Grant]:
    if not person.has_access:
        return {}
    return entitled_access(person)


def managed_group_ids(connection: Connection) -> set[str]:
    profile_groups = ProviderGroup.objects.filter(
        connection=connection, grants__isnull=False
    ).values_list("external_id", flat=True)
    team_groups = ProviderGroup.objects.filter(
        connection=connection, teams__isnull=False
    ).values_list("external_id", flat=True)
    return set(profile_groups) | set(team_groups)


def snapshot(person: Person) -> dict:
    """The JSON form of desired access a workflow keeps."""
    return {
        str(connection_id): {
            "connection": grant.connection.name,
            "groups": grant.group_ids,
            "group_names": {gid: g.label for gid, g in grant.groups.items()},
        }
        for connection_id, grant in desired_access(person).items()
    }


def team_group_usage(team: Team) -> dict[int, list[ProviderGroup]]:
    usage: dict[int, list[ProviderGroup]] = {}
    for group in team.groups.select_related("connection"):
        usage.setdefault(group.connection_id, []).append(group)
    return usage

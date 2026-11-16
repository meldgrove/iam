"""Teams and access profiles: the policy that decides desired access."""

from django.contrib import messages
from django.db import transaction
from django.db.models import Count, ProtectedError
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from .. import audit
from ..adapters import AdapterKind
from ..forms import AccessProfileForm, TeamForm
from ..models import AccessProfile, Connection, ProfileGrant, ProviderGroup, Team
from ..permissions import admin_required, viewer_required


def _group_connections():
    """Enabled account connections with their selectable groups."""
    rows = []
    for connection in Connection.objects.prefetch_related("groups"):
        if connection.kind != AdapterKind.PROVISIONING:
            continue
        rows.append(
            {
                "connection": connection,
                "groups": [g for g in connection.groups.all() if not g.missing],
                "group_label": connection.adapter_class.group_label,
                "supports_groups": connection.supports_groups,
            }
        )
    return rows


def _selected_groups(request, connection_ids=None):
    ids = [int(v) for v in request.POST.getlist("groups") if v.isdigit()]
    groups = ProviderGroup.objects.filter(pk__in=ids)
    if connection_ids is not None:
        groups = groups.filter(connection_id__in=connection_ids)
    return list(groups)


# -- teams --------------------------------------------------------------------------


@viewer_required
def team_list(request):
    teams = Team.objects.annotate(member_count=Count("members", distinct=True)).prefetch_related(
        "groups__connection"
    )
    return render(request, "iam/teams/list.html", {"teams": teams})


@viewer_required
def team_detail(request, pk):
    team = get_object_or_404(Team, pk=pk)
    return render(
        request,
        "iam/teams/detail.html",
        {
            "team": team,
            "members": team.members.exclude(status="archived").select_related("access_profile"),
            "groups": team.groups.select_related("connection"),
        },
    )


@admin_required
def team_form(request, pk=None):
    team = get_object_or_404(Team, pk=pk) if pk else None
    form = TeamForm(request.POST or None, instance=team)
    if request.method == "POST" and form.is_valid():
        with transaction.atomic():
            team = form.save()
            team.groups.set(_selected_groups(request))
            audit.record(
                "team.saved",
                f"Saved team {team.name}",
                actor=request.user,
                target=team,
                details={"groups": [g.label for g in team.groups.all()]},
            )
        messages.success(request, f"Saved team {team.name}.")
        return redirect("team_detail", pk=team.pk)
    selected = set(team.groups.values_list("pk", flat=True)) if team else set()
    return render(
        request,
        "iam/teams/form.html",
        {"form": form, "team": team, "connections": _group_connections(), "selected": selected},
    )


@admin_required
@require_POST
def team_delete(request, pk):
    team = get_object_or_404(Team, pk=pk)
    name = team.name
    team.delete()
    audit.record("team.deleted", f"Deleted team {name}", actor=request.user)
    messages.success(request, f"Deleted team {name}.")
    return redirect("team_list")


# -- access profiles --------------------------------------------------------------


@viewer_required
def profile_list(request):
    profiles = AccessProfile.objects.annotate(
        people_count=Count("people", distinct=True)
    ).prefetch_related("grants__connection", "grants__groups")
    return render(request, "iam/profiles/list.html", {"profiles": profiles})


@viewer_required
def profile_detail(request, pk):
    profile = get_object_or_404(AccessProfile, pk=pk)
    return render(
        request,
        "iam/profiles/detail.html",
        {
            "profile": profile,
            "grants": profile.grants.select_related("connection").prefetch_related("groups"),
            "people": profile.people.exclude(status="archived").select_related("team")[:200],
        },
    )


@admin_required
def profile_form(request, pk=None):
    profile = get_object_or_404(AccessProfile, pk=pk) if pk else None
    form = AccessProfileForm(request.POST or None, instance=profile)
    connections = _group_connections()
    if request.method == "POST" and form.is_valid():
        granted = {int(v) for v in request.POST.getlist("grant") if v.isdigit()} & {
            row["connection"].pk for row in connections
        }
        with transaction.atomic():
            profile = form.save()
            profile.grants.exclude(connection_id__in=granted).delete()
            groups = _selected_groups(request, granted)
            for connection_id in granted:
                grant, _ = ProfileGrant.objects.get_or_create(
                    profile=profile, connection_id=connection_id
                )
                grant.groups.set([g for g in groups if g.connection_id == connection_id])
            audit.record(
                "profile.saved",
                f"Saved access profile {profile.name}",
                actor=request.user,
                target=profile,
                details={
                    "grants": {
                        g.connection.name: [x.label for x in g.groups.all()]
                        for g in profile.grants.select_related("connection")
                    }
                },
            )
        messages.success(
            request,
            f"Saved {profile.name}. Start access updates for its people to apply changes.",
        )
        return redirect("profile_detail", pk=profile.pk)
    grants = {}
    if profile:
        for grant in profile.grants.prefetch_related("groups"):
            grants[grant.connection_id] = {g.pk for g in grant.groups.all()}
    for row in connections:
        row["granted"] = row["connection"].pk in grants
        row["selected"] = grants.get(row["connection"].pk, set())
    return render(
        request,
        "iam/profiles/form.html",
        {"form": form, "profile": profile, "connections": connections},
    )


@admin_required
@require_POST
def profile_delete(request, pk):
    profile = get_object_or_404(AccessProfile, pk=pk)
    name = profile.name
    try:
        profile.delete()
    except ProtectedError:
        messages.error(request, f"{name} is still assigned to people.")
        return redirect("profile_detail", pk=pk)
    audit.record("profile.deleted", f"Deleted access profile {name}", actor=request.user)
    messages.success(request, f"Deleted access profile {name}.")
    return redirect("profile_list")

"""Admin section: connections, app users and settings."""

from django.conf import settings
from django.contrib import messages
from django.db import transaction
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from .. import audit, updates
from ..adapters import AdapterKind, adapter_classes, get_adapter_class
from ..adapters.base import AdapterError
from ..crypto import SecretUnreadable
from ..forms import ConnectionForm, SettingsForm, UserForm
from ..models import AppSettings, Connection, ProviderGroup, Role, User
from ..permissions import admin_required
from ..sync import can_compare_accounts, compare_accounts, import_groups, import_people


@admin_required
def connection_list(request):
    connections = list(Connection.objects.all())
    by_kind = {kind: [] for kind in AdapterKind.LABELS}
    for connection in connections:
        by_kind.setdefault(connection.kind or AdapterKind.PROVISIONING, []).append(connection)
    return render(
        request,
        "iam/admin/connections/list.html",
        {
            "sections": [(AdapterKind.LABELS[k], v) for k, v in by_kind.items()],
            "has_connections": bool(connections),
        },
    )


@admin_required
def connection_choose(request):
    sections = {kind: [] for kind in AdapterKind.LABELS}
    for cls in adapter_classes():
        sections[cls.kind].append(cls)
    return render(
        request,
        "iam/admin/connections/choose.html",
        {"sections": [(AdapterKind.LABELS[k], v) for k, v in sections.items()]},
    )


@admin_required
def connection_create(request, adapter):
    cls = get_adapter_class(adapter)
    if cls is None:
        raise Http404
    form = ConnectionForm(cls, request.POST or None)
    if request.method == "POST" and form.is_valid():
        connection = form.save()
        audit.record(
            "connection.created",
            f"Added connection {connection.name} ({cls.name})",
            actor=request.user,
            target=connection,
            details={"settings_changed": form.changed_setting_names()},
        )
        messages.success(request, f"Added {connection.name}. Test it before turning it on.")
        return redirect("connection_detail", pk=connection.pk)
    return render(request, "iam/admin/connections/form.html", {"form": form, "adapter": cls})


@admin_required
def connection_detail(request, pk):
    connection = get_object_or_404(Connection, pk=pk)
    cls = connection.adapter_class
    return render(
        request,
        "iam/admin/connections/detail.html",
        {
            "connection": connection,
            "adapter": cls,
            "settings_rows": [
                (spec, connection.settings.get(spec.name))
                for spec in (cls.fields if cls else ())
                if not spec.secret
            ],
            "secret_rows": [
                (spec, spec.name in _saved_secrets(connection))
                for spec in (cls.fields if cls else ())
                if spec.secret
            ],
            "groups": connection.groups.all(),
            "sync_runs": connection.sync_runs.select_related("triggered_by")[:10],
            "accounts": connection.accounts.select_related("person")[:500],
            "account_count": connection.accounts.count(),
            "can_compare": cls is not None and can_compare_accounts(connection),
            "grants": connection.grants.select_related("profile"),
        },
    )


def _saved_secrets(connection) -> set:
    try:
        return set(connection.secrets())
    except SecretUnreadable:
        return set()


@admin_required
def connection_edit(request, pk):
    connection = get_object_or_404(Connection, pk=pk)
    cls = connection.adapter_class
    if cls is None:
        raise Http404
    form = ConnectionForm(cls, request.POST or None, instance=connection)
    if request.method == "POST" and form.is_valid():
        changed = form.changed_setting_names()
        connection = form.save()
        audit.record(
            "connection.updated",
            f"Changed connection {connection.name}",
            actor=request.user,
            target=connection,
            details={"settings_changed": changed},
        )
        messages.success(request, f"Saved {connection.name}.")
        return redirect("connection_detail", pk=connection.pk)
    return render(
        request,
        "iam/admin/connections/form.html",
        {"form": form, "adapter": cls, "connection": connection},
    )


@admin_required
@require_POST
def connection_test(request, pk):
    connection = get_object_or_404(Connection, pk=pk)
    try:
        result = connection.build_adapter().test_connection()
        ok, message = result.ok, result.message
    except (AdapterError, SecretUnreadable) as exc:
        ok, message = False, str(exc)
    except Exception as exc:  # noqa: BLE001 - shown to the admin, details in the log
        import logging

        logging.getLogger(__name__).exception("Connection test %s failed", connection.pk)
        ok, message = False, f"Unexpected error ({type(exc).__name__})."
    connection.status = Connection.Status.OK if ok else Connection.Status.ERROR
    connection.status_message = message[:2000]
    connection.checked_at = timezone.now()
    connection.save(update_fields=["status", "status_message", "checked_at"])
    audit.record(
        "connection.tested",
        f"Tested {connection.name}: {'connected' if ok else 'error'}",
        actor=request.user,
        target=connection,
    )
    (messages.success if ok else messages.error)(request, message)
    return redirect("connection_detail", pk=pk)


@admin_required
@require_POST
def connection_toggle(request, pk):
    connection = get_object_or_404(Connection, pk=pk)
    connection.enabled = not connection.enabled
    connection.save(update_fields=["enabled", "updated_at"])
    state = "on" if connection.enabled else "off"
    audit.record(
        "connection.toggled",
        f"Turned {connection.name} {state}",
        actor=request.user,
        target=connection,
    )
    messages.success(request, f"Turned {connection.name} {state}.")
    return redirect("connection_detail", pk=pk)


@admin_required
@require_POST
def connection_import_groups(request, pk):
    connection = get_object_or_404(Connection, pk=pk)
    run = import_groups(connection, actor=request.user)
    _report_run(request, run)
    return redirect("connection_detail", pk=pk)


@admin_required
@require_POST
def connection_sync(request, pk):
    connection = get_object_or_404(Connection, pk=pk)
    if connection.kind == AdapterKind.HR_SOURCE:
        run = import_people(connection, actor=request.user)
    else:
        run = compare_accounts(connection, actor=request.user)
    _report_run(request, run)
    return redirect("connection_detail", pk=pk)


def _report_run(request, run):
    if run.status == run.Status.SUCCEEDED:
        messages.success(request, f"{run.get_kind_display()} finished.")
    else:
        messages.error(request, f"{run.get_kind_display()} failed: {run.error}")


@admin_required
@require_POST
def connection_add_group(request, pk):
    connection = get_object_or_404(Connection, pk=pk)
    cls = connection.adapter_class
    name = request.POST.get("name", "").strip()[:255]
    if not cls or not cls.manual_groups or not name:
        messages.error(request, "Enter a name.")
        return redirect("connection_detail", pk=pk)
    group, created = ProviderGroup.objects.get_or_create(
        connection=connection,
        # Lowercasing can lengthen a name; the ID must fit in 255 characters.
        external_id=f"manual:{name.lower()[:248]}",
        defaults={"name": name, "manual": True},
    )
    if created:
        audit.record(
            "group.added",
            f"Added {name} to {connection.name}",
            actor=request.user,
            target=group,
        )
    return redirect("connection_detail", pk=pk)


@admin_required
@require_POST
def connection_remove_group(request, pk, group_pk):
    group = get_object_or_404(ProviderGroup, pk=group_pk, connection_id=pk, manual=True)
    if group.grants.exists() or group.teams.exists():
        messages.error(request, f"{group.name} is still used by an access profile or team.")
    else:
        group.delete()
        audit.record("group.removed", f"Removed {group.name}", actor=request.user)
    return redirect("connection_detail", pk=pk)


@admin_required
@require_POST
def connection_delete(request, pk):
    connection = get_object_or_404(Connection, pk=pk)
    if connection.jobs.exists() or connection.grants.exists():
        messages.error(
            request,
            "This connection has jobs or is used by access profiles. Turn it off instead.",
        )
        return redirect("connection_detail", pk=pk)
    name = connection.name
    connection.delete()
    audit.record("connection.deleted", f"Deleted connection {name}", actor=request.user)
    messages.success(request, f"Deleted {name}.")
    return redirect("connection_list")


# -- users -----------------------------------------------------------------------


@admin_required
def user_list(request):
    return render(
        request, "iam/admin/users/list.html", {"users": User.objects.select_related("person")}
    )


@admin_required
def user_form(request, pk=None):
    user = get_object_or_404(User, pk=pk) if pk else None
    form = UserForm(request.POST or None, instance=user)
    if request.method == "POST" and form.is_valid():
        with transaction.atomic():
            edited = form.save(commit=False)
            if edited.pk == request.user.pk and (not edited.is_active or edited.role != Role.ADMIN):
                messages.error(request, "You cannot remove your own admin access.")
                return redirect("user_list")
            if user is None:
                edited.set_unusable_password()
            if "role" in form.changed_data:
                edited.role_source = User.RoleSource.LOCAL
            edited.save()
            audit.record(
                "user.saved",
                f"Saved app user {edited.email} ({edited.get_role_display()})",
                actor=request.user,
                target=edited,
                details={"fields": sorted(form.changed_data)},
            )
        messages.success(request, f"Saved {edited.email}.")
        return redirect("user_list")
    return render(request, "iam/admin/users/form.html", {"form": form, "edited": user})


# -- settings ---------------------------------------------------------------------


@admin_required
def settings_form(request):
    instance = AppSettings.load()
    form = SettingsForm(request.POST or None, instance=instance)
    if request.method == "POST" and form.is_valid():
        form.save()
        audit.record(
            "settings.saved",
            "Changed settings",
            actor=request.user,
            details={"fields": sorted(form.changed_data)},
        )
        messages.success(request, "Saved the settings.")
        return redirect("settings")
    return render(request, "iam/admin/settings.html", {"form": form, "updates": _updates_section()})


def _updates_section() -> dict:
    """The "Updates" section: installed version, channel, automatic installs."""
    result = updates.status()
    updater = result.updater or {}
    channel = updater.get("channel") if updater.get("channel") in updates.CHANNELS else "stable"
    return {
        "installed": settings.APP_VERSION or "development",
        "reachable": result.reachable,
        "error": result.error,
        "channel": channel,
        "channel_label": channel.capitalize(),
        "locked": updater.get("channelLocked") or "",
        "cooldown": updates.describe_duration(updater.get("cooldown")),
        "automatic": updates.automatic_installs(updater) if result.reachable else "",
    }

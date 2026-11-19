"""Admin routes for in-app updates (docs/updates.md). The update button in the
top bar and the "Updates" section of Admin › Settings reach the Meldgrove
Updater only through these; each answers UpdatesResponse as JSON."""

import json

from django.contrib import messages
from django.http import JsonResponse
from django.shortcuts import redirect
from django.urls import reverse
from django.views.decorators.http import require_GET, require_POST

from .. import audit, updates
from ..permissions import admin_required

CHANNEL_LABELS = {"stable": "Stable", "preview": "Preview"}


def _answer(result: updates.Result) -> JsonResponse:
    return JsonResponse(result.body(), status=result.http_status)


def _wants_json(request) -> bool:
    return request.content_type == "application/json"


def _field(request, name: str):
    """A field of the button's JSON body, or of the settings form."""
    if not _wants_json(request):
        return request.POST.get(name)
    try:
        body = json.loads(request.body or b"{}")
    except ValueError:
        return None
    return body.get(name) if isinstance(body, dict) else None


@admin_required
@require_GET
def status(request):
    return _answer(updates.status())


@admin_required
@require_POST
def check(request):
    return _answer(updates.check())


@admin_required
@require_POST
def install(request):
    version = _field(request, "version")
    result = updates.install(version)
    if result.accepted:
        audit.record(
            "updates.install",
            f"Requested the installation of Meldgrove IAM {version}",
            actor=request.user,
            details={"version": version},
        )
    return _answer(result)


@admin_required
@require_POST
def channel(request):
    channel = _field(request, "channel")
    result = updates.set_channel(channel)
    if result.accepted:
        audit.record(
            "updates.channel",
            f"Switched the update channel to {CHANNEL_LABELS[channel]}",
            actor=request.user,
            details={"channel": channel},
        )
    if _wants_json(request):
        return _answer(result)
    # The settings form posts here without JavaScript: back to its section.
    if result.accepted:
        messages.success(request, f"Switched the update channel to {CHANNEL_LABELS[channel]}.")
    else:
        messages.error(request, result.error or "The update service refused the channel.")
    return redirect(reverse("settings") + "#updates")

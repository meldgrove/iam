"""The audit log. Every change by a person or the worker writes one event."""

from __future__ import annotations

from .models import AuditEvent, Person

WORKER_LABEL = "Worker"


def record(
    action: str,
    summary: str,
    *,
    actor=None,
    person: Person | None = None,
    target=None,
    details: dict | None = None,
) -> AuditEvent:
    label = WORKER_LABEL
    if actor is not None:
        label = getattr(actor, "email", "") or str(actor)
    return AuditEvent.objects.create(
        actor=actor if actor is not None and getattr(actor, "pk", None) else None,
        actor_label=label[:255],
        action=action,
        summary=summary[:500],
        person=person,
        target_type=type(target).__name__.lower() if target is not None else "",
        target_id=str(target.pk) if target is not None and target.pk else "",
        details=details or {},
    )

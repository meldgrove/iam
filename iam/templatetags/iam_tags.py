from django import template
from django.utils.html import format_html

register = template.Library()

TONES = {
    "ok": {"active", "succeeded", "completed", "done", "ok"},
    "info": {"onboarding", "running", "queued", "planned", "untested", "unknown", "rehearsed"},
    "warn": {"held", "manual", "waiting", "offboarding", "open", "missing"},
    "danger": {"failed", "error"},
}


@register.simple_tag
def badge(value, label=None):
    value = str(value or "")
    tone = next((name for name, values in TONES.items() if value in values), "")
    return format_html('<span class="badge {}">{}</span>', tone, label or value.replace("_", " "))


@register.filter
def get_item(mapping, key):
    try:
        return mapping.get(key)
    except AttributeError:
        return None


@register.filter
def has(collection, value):
    return value in collection


@register.simple_tag(takes_context=True)
def nav_active(context, *prefixes):
    path = context["request"].path
    return "active" if any(path == p if p == "/" else path.startswith(p) for p in prefixes) else ""


@register.filter
def summarize(summary):
    """One line from a sync run summary: lists count, numbers show."""
    parts = []
    for key, value in (summary or {}).items():
        label = key.replace("_", " ")
        if isinstance(value, list | dict):
            if value:
                parts.append(f"{label}: {len(value)}")
        else:
            parts.append(f"{label}: {value}")
    return " · ".join(parts)

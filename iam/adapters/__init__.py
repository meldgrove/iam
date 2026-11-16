"""Registry of the adapters admins can connect in Admin > Connections."""

from __future__ import annotations

from importlib import import_module

from .base import Adapter, AdapterKind

# module path relative to this package, class name
_ADAPTERS = {
    "keycloak": ("keycloak", "KeycloakAdapter"),
    "google_workspace": ("google_workspace", "GoogleWorkspaceAdapter"),
    "scim": ("scim", "ScimAdapter"),
    "hubspot": ("hubspot", "HubSpotAdapter"),
    "slack": ("slack", "SlackAdapter"),
    "notion": ("notion", "NotionAdapter"),
    "relution": ("relution", "RelutionAdapter"),
    "manual": ("manual", "ManualAdapter"),
    "personio": ("personio", "PersonioAdapter"),
    "smtp": ("smtp", "SmtpAdapter"),
}


def get_adapter_class(slug: str) -> type[Adapter] | None:
    entry = _ADAPTERS.get(slug)
    if entry is None:
        return None
    # Entries with a dot are absolute module paths (used by tests).
    path = entry[0] if "." in entry[0] else f"{__name__}.{entry[0]}"
    module = import_module(path)
    return getattr(module, entry[1])


def adapter_classes() -> list[type[Adapter]]:
    return [cls for slug in _ADAPTERS if (cls := get_adapter_class(slug)) is not None]


def adapter_choices() -> list[tuple[str, str]]:
    return [(cls.slug, cls.name) for cls in adapter_classes()]


__all__ = ["Adapter", "AdapterKind", "adapter_choices", "adapter_classes", "get_adapter_class"]

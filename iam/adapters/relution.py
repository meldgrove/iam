"""Relution (mobile device management): users and group memberships.

API contract, as verified against the Relution cloud:

- Every request authenticates with the header ``X-User-Access-Token``. Use the
  token of a Relution user that may only look up the organization's users and
  manage their group memberships.
- Users live at ``/api/management/v1/security/users``, groups at
  ``/api/management/v1/security/groups``. Listings take ``limit``/``offset``
  and answer with an ``items`` array. There is no verified server-side email
  filter, so lookups page through the organization's roster.
- A shared Relution cloud can return entities of other organizations; items
  whose ``organizationUuid`` differs from the configured one are ignored.
- The API rejects PATCH. Updates are full-object PUTs, so the adapter merges
  its change over the fetched user and drops any password, credential, secret
  or token fields before sending it back.
- User objects carry ``activated``/``locked``/``ssoOnly`` and do not embed
  group memberships. Memberships are read with GET and replaced with PUT on
  ``/users/<uuid>/groups``; the PUT body is a bare JSON array of group UUIDs.

Disabling sets ``activated`` to false. The adapter never deletes users:
deleting a Relution user also affects the devices enrolled for that user,
which IT should retire on purpose, not as a side effect of offboarding. For
the same reason memberships stay in place when an account is disabled, so
group-based policies and apps are not pulled from devices still in use.
The adapter never creates groups and never sets a password: new users are
SSO-only by default and sign in through the organization's identity provider.
"""

from __future__ import annotations

from collections.abc import Iterable
from urllib.parse import quote, urlsplit

import httpx

from .base import (
    SYNC_INTERVAL_FIELD,
    AccountSpec,
    Adapter,
    AdapterError,
    ConfigurationError,
    EnsureResult,
    ExternalAccount,
    ExternalGroup,
    Field,
    TestResult,
    TransientError,
)

USERS_PATH = "/api/management/v1/security/users"
GROUPS_PATH = "/api/management/v1/security/groups"
PAGE_SIZE = 200
MAX_PAGES = 100
MAX_RESPONSE_BYTES = 5 * 1024 * 1024
SENSITIVE_KEY_PARTS = ("password", "credential", "secret", "token")
INACTIVE_STATES = {"disabled", "inactive", "suspended", "archived"}
LOCAL_HOSTS = {"localhost", "127.0.0.1"}


def _flag(value) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _normalize(value: str) -> str:
    return (value or "").strip().lower()


def _entity_id(item: dict) -> str:
    value = item.get("uuid") or item.get("id")
    return str(value) if value else ""


def user_is_active(user: dict) -> bool:
    if (
        user.get("enabled") is False
        or user.get("active") is False
        or user.get("activated") is False
        or user.get("locked") is True
    ):
        return False
    status = str(user.get("status") or user.get("state") or "").lower()
    return status not in INACTIVE_STATES


def membership_ids(data) -> set[str] | None:
    """Group IDs from a membership response, or None when it holds none of
    the known shapes."""
    if isinstance(data, list):
        raw = data
    elif isinstance(data, dict):
        present = [key for key in ("groups", "roles", "userGroups", "items") if key in data]
        if not present:
            return None
        raw = next((data[key] for key in present if data[key]), [])
    else:
        return None
    if not isinstance(raw, list):
        return None
    ids = set()
    for entry in raw:
        value = _entity_id(entry) if isinstance(entry, dict) else entry
        if value:
            ids.add(str(value))
    return ids


def update_payload(user: dict, changes: dict) -> dict:
    """A full-object PUT body that never replays credential material."""
    return {
        key: value
        for key, value in {**user, **changes}.items()
        if not any(part in key.casefold() for part in SENSITIVE_KEY_PARTS)
    }


class RelutionAdapter(Adapter):
    slug = "relution"
    name = "Relution"
    description = (
        "Mobile device management. Creates SSO-only users, keeps their group "
        "memberships, and deactivates users on offboarding. Never deletes users, so "
        "enrolled devices are not affected."
    )
    supports_groups = True
    remove_groups_on_disable = False
    group_label = "Relution groups"
    fields = (
        Field(
            "base_url",
            "Relution address",
            kind="url",
            required=True,
            destination=True,
            placeholder="https://relution.example.invalid",
            help="The origin only, without a path.",
        ),
        Field(
            "organization_uuid",
            "Organization UUID",
            required=True,
            help="Users are created in this organization; others are ignored.",
        ),
        Field(
            "api_token",
            "API access token",
            kind="secret",
            required=True,
            help=(
                "Token of a Relution user that may only manage the organization's users "
                "and group memberships."
            ),
        ),
        Field(
            "sso_only",
            "Sign in only through single sign-on",
            kind="bool",
            default=True,
            help="New users get no Relution password.",
        ),
        Field(
            "managed_apple_id",
            "Use the work email as managed Apple ID",
            kind="bool",
            default=True,
        ),
        SYNC_INTERVAL_FIELD,
    )

    # -- HTTP ------------------------------------------------------------------

    def _base_url(self) -> str:
        self.require("base_url", "organization_uuid", "api_token")
        base_url = str(self.setting("base_url")).rstrip("/")
        parts = urlsplit(base_url)
        if (
            not parts.netloc
            or parts.username
            or parts.password
            or parts.query
            or parts.fragment
            or parts.path not in {"", "/"}
        ):
            raise ConfigurationError(
                "The Relution address must be an origin without credentials or a path."
            )
        if parts.scheme != "https" and not (
            parts.scheme == "http" and parts.hostname in LOCAL_HOSTS
        ):
            raise ConfigurationError("The Relution address must use HTTPS.")
        return base_url

    @property
    def organization_uuid(self) -> str:
        return str(self.setting("organization_uuid", ""))

    def _client(self) -> httpx.Client:
        return self.http(
            self._base_url(),
            headers={
                "Accept": "application/json",
                "X-User-Access-Token": str(self.setting("api_token")),
            },
        )

    def _request(self, method: str, path: str, what: str, *, allow_404: bool = False, **kwargs):
        """Send one request; return the parsed JSON, None for an empty body,
        or None for 404 when `allow_404`."""
        with self._client() as client:
            try:
                response = client.request(method, path, **kwargs)
            except httpx.TimeoutException as exc:
                raise TransientError(f"Relution did not answer in time ({what}).") from exc
            except httpx.TransportError as exc:
                raise TransientError(f"Could not reach Relution ({what}).") from exc
        if allow_404 and response.status_code == 404:
            return None
        # Provider bodies can echo request fields; only the status is kept.
        self.raise_for_status(response, what)
        content = response.content or b""
        if len(content) > MAX_RESPONSE_BYTES:
            raise AdapterError(f"Relution's answer was too large ({what}).")
        if response.status_code == 204 or not content:
            return None
        if "json" not in response.headers.get("Content-Type", "").lower():
            raise AdapterError(f"Relution did not answer with JSON ({what}).")
        try:
            return response.json()
        except ValueError as exc:
            raise AdapterError(f"Relution answered with malformed JSON ({what}).") from exc

    def _page(self, data, what: str) -> list[dict]:
        if isinstance(data, list):
            results = data
        elif isinstance(data, dict):
            results = data.get("items", data.get("results"))
        else:
            results = None
        if not isinstance(results, list) or any(not isinstance(i, dict) for i in results):
            raise AdapterError(f"Relution answered in an unexpected shape ({what}).")
        return results

    def _in_organization(self, item: dict) -> bool:
        # Entities without the field cannot be scoped; the token is bound to
        # one organization, so they are kept.
        value = str(item.get("organizationUuid") or "")
        return not value or value.lower() == self.organization_uuid.lower()

    def _list(self, path: str, what: str) -> list[dict]:
        items: list[dict] = []
        for page in range(MAX_PAGES):
            data = self._request(
                "GET", path, what, params={"limit": PAGE_SIZE, "offset": page * PAGE_SIZE}
            )
            results = self._page(data, what)
            items.extend(item for item in results if self._in_organization(item))
            if len(results) < PAGE_SIZE:
                return items
        raise AdapterError(f"Relution listed more than {MAX_PAGES * PAGE_SIZE} entries ({what}).")

    # -- users -----------------------------------------------------------------

    def _user_path(self, user_id: str) -> str:
        return f"{USERS_PATH}/{quote(user_id, safe='')}"

    def _get_user(self, user_id: str) -> dict | None:
        data = self._request("GET", self._user_path(user_id), "read a user", allow_404=True)
        if data is None:
            return None
        if not isinstance(data, dict) or not _entity_id(data):
            raise AdapterError("Relution answered in an unexpected shape (read a user).")
        return data

    def _to_account(self, user: dict) -> ExternalAccount:
        return ExternalAccount(
            external_id=_entity_id(user),
            username=str(user.get("name") or ""),
            email=_normalize(str(user.get("email") or "")),
            display_name=" ".join(
                part for part in (user.get("givenName"), user.get("surname")) if part
            ),
            active=user_is_active(user),
            groups=frozenset(ids) if (ids := membership_ids_embedded(user)) is not None else None,
        )

    def _match(self, spec: AccountSpec) -> dict | None:
        email = _normalize(spec.email)
        username = _normalize(spec.username)
        users = [u for u in self._list(USERS_PATH, "list users") if _entity_id(u)]
        matches: list[dict] = []
        if email:
            matches = [
                u
                for u in users
                if email
                in {_normalize(str(u.get("email") or "")), _normalize(str(u.get("name") or ""))}
            ]
        if not matches and username:
            matches = [u for u in users if _normalize(str(u.get("name") or "")) == username]
        if len(matches) > 1:
            raise AdapterError(
                "Several Relution users match this person. Link the right one in Relution."
            )
        return matches[0] if matches else None

    def find_account(self, spec: AccountSpec, external_id: str = "") -> ExternalAccount | None:
        if external_id:
            user = self._get_user(external_id)
            if user is not None:
                # The stored ID proves ownership even after an email change.
                return self._to_account(user)
        user = self._match(spec)
        return self._to_account(user) if user else None

    def create_account(self, spec: AccountSpec) -> EnsureResult:
        email = _normalize(spec.email)
        if not email:
            raise AdapterError("A Relution account needs the person's work email.")
        payload = {
            "organizationUuid": self.organization_uuid,
            "name": email,
            "email": email,
            "givenName": spec.first_name,
            "surname": spec.last_name,
            "ssoOnly": _flag(self.setting("sso_only", True)),
            "activated": True,
        }
        if _flag(self.setting("managed_apple_id", True)):
            payload["managedAppleId"] = email
        try:
            created = self._request("POST", USERS_PATH, "create a user", json=payload)
        except TransientError:
            # A create that timed out or failed on the server may still have
            # been stored. Read before a retry can create a duplicate.
            user = self._match(spec)
            if user is None:
                raise
        else:
            user = created if isinstance(created, dict) and _entity_id(created) else None
            user = user or self._match(spec)
        if user is None:
            raise AdapterError("Relution did not return the new user.")
        account = self._to_account(user)
        account.groups = frozenset()
        return EnsureResult(account=account, created=True)

    def set_active(self, account: ExternalAccount, active: bool) -> ExternalAccount:
        user = self._get_user(account.external_id)
        if user is None:
            if not active:
                # Already gone: nothing left that could sign in.
                account.active = False
                return account
            raise AdapterError("The Relution user no longer exists.")
        if user_is_active(user) != active:
            self._request(
                "PUT",
                self._user_path(account.external_id),
                "activate a user" if active else "deactivate a user",
                allow_404=not active,
                json=update_payload(user, {"activated": active}),
            )
        confirmed = self._get_user(account.external_id)
        if confirmed is None:
            if not active:
                account.active = False
                return account
            raise AdapterError("The Relution user disappeared while it was activated.")
        if user_is_active(confirmed) != active:
            raise AdapterError(
                "Relution did not confirm the user as active; it may be locked in Relution."
                if active
                else "Relution did not confirm the user as deactivated."
            )
        updated = self._to_account(confirmed)
        updated.groups = account.groups
        return updated

    # -- groups ----------------------------------------------------------------

    def list_groups(self) -> list[ExternalGroup]:
        groups = []
        for group in self._list(GROUPS_PATH, "list groups"):
            group_id = _entity_id(group)
            if not group_id:
                continue
            groups.append(
                ExternalGroup(
                    external_id=group_id,
                    name=str(group.get("name") or group_id).strip(),
                    description=str(group.get("description") or ""),
                )
            )
        return groups

    def _membership(self, user_id: str) -> set[str]:
        data = self._request("GET", f"{self._user_path(user_id)}/groups", "read group memberships")
        ids = membership_ids(data)
        if ids is None:
            raise AdapterError("Relution answered in an unexpected shape (read group memberships).")
        return ids

    def _replace_membership(self, user_id: str, group_ids: set[str], what: str) -> set[str]:
        self._request("PUT", f"{self._user_path(user_id)}/groups", what, json=sorted(group_ids))
        return self._membership(user_id)

    def account_groups(self, account: ExternalAccount) -> set[str]:
        if account.groups is not None:
            return set(account.groups)
        return self._membership(account.external_id)

    def add_to_group(self, account: ExternalAccount, group_id: str) -> None:
        current = self._membership(account.external_id)
        if group_id in current:
            return
        confirmed = self._replace_membership(
            account.external_id, current | {group_id}, "add a group membership"
        )
        if group_id not in confirmed:
            raise AdapterError("Relution did not confirm the new group membership.")

    def remove_from_group(self, account: ExternalAccount, group_id: str) -> None:
        current = self._membership(account.external_id)
        if group_id not in current:
            return
        confirmed = self._replace_membership(
            account.external_id, current - {group_id}, "remove a group membership"
        )
        if group_id in confirmed:
            raise AdapterError("Relution did not confirm the removed group membership.")

    # -- reads -----------------------------------------------------------------

    def list_accounts(self) -> Iterable[ExternalAccount]:
        # Listings do not embed memberships, so groups stay unknown (None).
        return [self._to_account(u) for u in self._list(USERS_PATH, "list users") if _entity_id(u)]

    def test_connection(self) -> TestResult:
        for path, what in ((USERS_PATH, "list users"), (GROUPS_PATH, "list groups")):
            self._page(self._request("GET", path, what, params={"limit": 1, "offset": 0}), what)
        return TestResult(True, "Connected. Relution lists users and groups for this token.")

    def manual_instructions(self, action: str, spec: AccountSpec, groups: list[str]) -> str:
        who = f"{spec.display_name} ({spec.email or spec.username})"
        if action == "disable_account":
            return (
                f"In Relution, deactivate the user of {who}. Do not delete the user: that "
                "also affects the enrolled devices. Retire devices separately."
            )
        lines = [
            f"In Relution, make sure {who} has an activated, SSO-only user with the work "
            "email as login name."
        ]
        if groups:
            lines.append("Relution groups: " + ", ".join(groups) + ".")
        return "\n".join(lines)


def membership_ids_embedded(user: dict) -> set[str] | None:
    """Memberships a user object carries itself; listings usually carry none."""
    if not any(key in user for key in ("groups", "roles", "userGroups")):
        return None
    return membership_ids({k: user[k] for k in ("groups", "roles", "userGroups") if k in user})

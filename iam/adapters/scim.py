"""Any app with a SCIM 2.0 endpoint (RFC 7643 and RFC 7644)."""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import replace
from urllib.parse import quote

import httpx

from .base import (
    SYNC_INTERVAL_FIELD,
    AccountSpec,
    Adapter,
    AdapterError,
    EnsureResult,
    ExternalAccount,
    ExternalGroup,
    Field,
    TestResult,
    TransientError,
)

USER_SCHEMA = "urn:ietf:params:scim:schemas:core:2.0:User"
PATCH_SCHEMA = "urn:ietf:params:scim:api:messages:2.0:PatchOp"
PAGE_SIZE = 100
MAX_PAGES = 1000
# Answers of servers that do not support a filter or a PATCH form.
UNSUPPORTED = (400, 501)


def _flag(value) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _filter_value(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


class ScimAdapter(Adapter):
    slug = "scim"
    name = "SCIM 2.0 app"
    description = (
        "Creates, disables and groups accounts in any app with a SCIM 2.0 endpoint, "
        "for example Atlassian, Zoom, Miro or GitHub Enterprise."
    )
    supports_groups = True
    fields = (
        Field(
            "base_url",
            "SCIM base URL",
            kind="url",
            required=True,
            destination=True,
            placeholder="https://app.example.invalid/scim/v2",
            help="The URL that /Users and /Groups follow.",
        ),
        Field("token", "Bearer token", kind="secret", required=True),
        Field(
            "user_name_source",
            "User name",
            kind="choice",
            default="email",
            choices=(("email", "Work email"), ("username", "Username")),
            help="What the app's userName holds.",
        ),
        Field(
            "deactivate_mode",
            "When access ends",
            kind="choice",
            default="active_false",
            choices=(
                ("active_false", "Deactivate the user (active = false)"),
                ("delete", "Delete the user"),
            ),
        ),
        Field(
            "groups_enabled",
            "Manage groups",
            kind="bool",
            default=True,
            help="Turn off for apps without a /Groups endpoint.",
        ),
        SYNC_INTERVAL_FIELD,
    )

    @classmethod
    def groups_enabled(cls, settings: dict) -> bool:
        return _flag(settings.get("groups_enabled", True))

    def __init__(self, config: dict, **kwargs):
        super().__init__(config, **kwargs)
        # Instance value: apps without /Groups manage accounts only.
        self.supports_groups = _flag(self.setting("groups_enabled", True))

    # -- HTTP -------------------------------------------------------------

    def _client(self) -> httpx.Client:
        self.require("base_url", "token")
        return self.http(
            str(self.setting("base_url")).rstrip("/"),
            headers={
                "Authorization": f"Bearer {self.setting('token')}",
                "Accept": "application/scim+json, application/json",
            },
        )

    def _send(
        self,
        method: str,
        url: str,
        what: str,
        *,
        allow: tuple[int, ...] = (),
        body: dict | None = None,
        params: dict | None = None,
    ) -> httpx.Response:
        kwargs: dict = {}
        if params:
            kwargs["params"] = params
        if body is not None:
            kwargs["content"] = json.dumps(body)
            kwargs["headers"] = {"Content-Type": "application/scim+json"}
        with self._client() as client:
            try:
                response = client.request(method, url, **kwargs)
            except httpx.TimeoutException as exc:
                raise TransientError(f"{self.name} did not answer in time ({what}).") from exc
            except httpx.TransportError as exc:
                raise TransientError(f"Could not reach {self.name} ({what}).") from exc
        if response.status_code in allow:
            return response
        self.raise_for_status(response, what)
        return response

    @staticmethod
    def _json(response: httpx.Response) -> dict:
        if response.status_code == 204 or not response.content:
            return {}
        try:
            data = response.json()
        except ValueError:
            return {}
        return data if isinstance(data, dict) else {}

    def _listing(self, response: httpx.Response, what: str) -> tuple[list[dict], object]:
        """The resources of a list response and its totalResults. Anything
        else is an error: read as an empty list, a maintenance page would
        mark every account and group missing."""
        try:
            data = response.json()
        except ValueError as exc:
            raise AdapterError(f"{self.name} sent an unreadable answer ({what}).") from exc
        resources = data.get("Resources") if isinstance(data, dict) else None
        if resources is None and isinstance(data, dict) and data.get("totalResults") == 0:
            resources = []  # RFC 7644 lets an empty list leave Resources out
        if not isinstance(resources, list):
            raise AdapterError(f"{self.name} sent an unexpected answer ({what}).")
        return [r for r in resources if isinstance(r, dict)], data.get("totalResults")

    # -- mapping ------------------------------------------------------------

    def _user_name(self, spec: AccountSpec) -> str:
        if self.setting("user_name_source") == "username":
            return spec.username or spec.email
        return spec.email or spec.username

    @staticmethod
    def _account(resource: dict) -> ExternalAccount:
        emails = resource.get("emails") or []
        email = ""
        for entry in emails:
            if isinstance(entry, dict) and entry.get("value"):
                if entry.get("primary") or not email:
                    email = entry["value"]
        display_name = resource.get("displayName") or ""
        name = resource.get("name") or {}
        if not display_name and isinstance(name, dict):
            display_name = " ".join(
                part for part in (name.get("givenName"), name.get("familyName")) if part
            )
        groups = None
        if isinstance(resource.get("groups"), list):
            groups = frozenset(
                str(g["value"])
                for g in resource["groups"]
                if isinstance(g, dict) and g.get("value")
            )
        active = resource.get("active")
        return ExternalAccount(
            external_id=str(resource.get("id", "")),
            username=resource.get("userName") or "",
            email=email,
            display_name=display_name,
            active=True if active is None else _flag(active),
            groups=groups,
        )

    @staticmethod
    def _values(resource: dict, attribute: str) -> set[str]:
        if attribute == "userName":
            return {str(resource.get("userName") or "").casefold()}
        return {
            str(entry.get("value") or "").casefold()
            for entry in resource.get("emails") or []
            if isinstance(entry, dict)
        }

    def _find_user(self, attribute: str, value: str, *, tolerate_unsupported: bool) -> dict | None:
        """The one user whose attribute is the value. The answer is checked:
        a server can ignore a filter and list everyone, and emails need not
        be unique."""
        response = self._send(
            "GET",
            "/Users",
            "look up a user",
            allow=UNSUPPORTED if tolerate_unsupported else (),
            params={"filter": f"{attribute} eq {_filter_value(value)}", "count": "2"},
        )
        if response.status_code in UNSUPPORTED:
            return None
        wanted = value.casefold()
        resources, _ = self._listing(response, "look up a user")
        matches = [r for r in resources if wanted in self._values(r, attribute)]
        if len(matches) > 1:
            raise AdapterError(
                f"Several {self.name} users have the {attribute} {value}. Nothing was changed."
            )
        return matches[0] if matches else None

    # -- connection -----------------------------------------------------------

    def test_connection(self) -> TestResult:
        try:
            config = self._send(
                "GET",
                "/ServiceProviderConfig",
                "read the service provider configuration",
                allow=(404,),
            )
            # The configuration may be public; listing one user proves the token.
            self._send("GET", "/Users", "list users", params={"count": "1"})
        except AdapterError as exc:
            return TestResult(False, str(exc))
        if config.status_code == 404:
            return TestResult(True, "Connected. The app lists users over SCIM.")
        patch = self._json(config).get("patch") or {}
        if isinstance(patch, dict) and patch.get("supported") is False:
            return TestResult(
                True,
                "Connected, but the app reports no PATCH support: disabling users and "
                "group changes may fail.",
            )
        return TestResult(True, "Connected to the SCIM endpoint.")

    # -- accounts ---------------------------------------------------------------

    def find_account(self, spec: AccountSpec, external_id: str = "") -> ExternalAccount | None:
        if external_id:
            response = self._send(
                "GET", f"/Users/{quote(external_id, safe='')}", "read a user", allow=(404,)
            )
            if response.status_code != 404:
                return self._account(self._json(response))
        user_name = self._user_name(spec)
        if user_name:
            resource = self._find_user("userName", user_name, tolerate_unsupported=False)
            if resource:
                return self._account(resource)
        # Also when the userName is the email: the user may have another userName.
        if spec.email:
            resource = self._find_user("emails.value", spec.email, tolerate_unsupported=True)
            if resource:
                return self._account(resource)
        return None

    def create_account(self, spec: AccountSpec) -> EnsureResult:
        user_name = self._user_name(spec)
        if not user_name:
            raise AdapterError("SCIM accounts need a work email or username.")
        body = {
            "schemas": [USER_SCHEMA],
            "userName": user_name,
            "name": {"givenName": spec.first_name, "familyName": spec.last_name},
            "displayName": spec.display_name,
            "active": True,
        }
        if spec.email:
            body["emails"] = [{"value": spec.email, "primary": True, "type": "work"}]
        if spec.job_title:
            body["title"] = spec.job_title
        response = self._send("POST", "/Users", "create a user", allow=(409,), body=body)
        if response.status_code == 409:
            account = self.find_account(spec)
            if account is None:
                raise AdapterError(
                    f"{self.name} reports that the user already exists, but no user "
                    "matches its user name or email."
                )
            result = EnsureResult(account=account, notes=["The user already existed."])
            if not account.active:
                result.account = self.set_active(account, True)
                result.reactivated = True
            return result
        return EnsureResult(account=self._account(self._json(response)), created=True)

    def set_active(self, account: ExternalAccount, active: bool) -> ExternalAccount:
        path = f"/Users/{quote(account.external_id, safe='')}"
        if not active and self.setting("deactivate_mode") == "delete":
            self._send("DELETE", path, "delete a user", allow=(404,))
            return replace(account, active=False, groups=frozenset())
        operations = [
            [{"op": "replace", "path": "active", "value": active}],
            # Some servers only accept the value form without a path.
            [{"op": "replace", "value": {"active": active}}],
        ]
        response = None
        for index, operation in enumerate(operations):
            last = index == len(operations) - 1
            response = self._send(
                "PATCH",
                path,
                "activate a user" if active else "deactivate a user",
                allow=() if last else UNSUPPORTED,
                body={"schemas": [PATCH_SCHEMA], "Operations": operation},
            )
            if response.status_code not in UNSUPPORTED:
                break
        data = self._json(response)
        if data.get("id"):
            updated = self._account(data)
            if updated.groups is None:
                updated.groups = account.groups
            return updated
        return replace(account, active=active)

    def list_accounts(self) -> Iterable[ExternalAccount]:
        for resource in self._paged("/Users", "list users"):
            yield self._account(resource)

    # -- groups -------------------------------------------------------------------

    def _paged(
        self, url: str, what: str, params: dict | None = None, *, unsupported: str = ""
    ) -> Iterable[dict]:
        """Every page of a listing. With `unsupported`, a server that refuses
        the request (400, 501) raises that message."""
        start = 1
        for _ in range(MAX_PAGES):
            response = self._send(
                "GET",
                url,
                what,
                allow=UNSUPPORTED if unsupported else (),
                params={**(params or {}), "startIndex": str(start), "count": str(PAGE_SIZE)},
            )
            if response.status_code in UNSUPPORTED and unsupported:
                raise AdapterError(unsupported)
            resources, total = self._listing(response, what)
            yield from resources
            start += len(resources)
            if not resources or (isinstance(total, int) and start > total):
                return

    def list_groups(self) -> list[ExternalGroup]:
        if not self.supports_groups:
            return []
        return [
            ExternalGroup(external_id=str(g["id"]), name=g.get("displayName") or str(g["id"]))
            for g in self._paged("/Groups", "list groups", {"attributes": "displayName"})
            if g.get("id")
        ]

    def account_groups(self, account: ExternalAccount) -> set[str]:
        if account.groups is not None:
            return set(account.groups)
        if not self.supports_groups:
            return set()
        # Unknown memberships must not read as none: nothing would be removed.
        groups = self._paged(
            "/Groups",
            "read a user's groups",
            {
                "filter": f"members.value eq {_filter_value(account.external_id)}",
                "attributes": "displayName",
            },
            unsupported=(
                f"{self.name} cannot list a user's groups (no members filter). Change the "
                "groups by hand, or turn off Manage groups for this connection."
            ),
        )
        return {str(g["id"]) for g in groups if g.get("id")}

    def add_to_group(self, account: ExternalAccount, group_id: str) -> None:
        self._send(
            "PATCH",
            f"/Groups/{quote(group_id, safe='')}",
            "add a user to a group",
            body={
                "schemas": [PATCH_SCHEMA],
                "Operations": [
                    {"op": "add", "path": "members", "value": [{"value": account.external_id}]}
                ],
            },
        )

    def remove_from_group(self, account: ExternalAccount, group_id: str) -> None:
        self._send(
            "PATCH",
            f"/Groups/{quote(group_id, safe='')}",
            "remove a user from a group",
            body={
                "schemas": [PATCH_SCHEMA],
                "Operations": [
                    {
                        "op": "remove",
                        "path": f"members[value eq {_filter_value(account.external_id)}]",
                    }
                ],
            },
        )

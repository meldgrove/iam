"""HubSpot users and teams, through the Settings Users API."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import replace
from urllib.parse import quote

import httpx

from .base import (
    SYNC_INTERVAL_FIELD,
    AccountPlan,
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

API_URL = "https://api.hubapi.com"
USERS_PATH = "/settings/users/2026-03"
NEEDS_EMAIL = "HubSpot accounts need a work email."
CANNOT_REACTIVATE = (
    "HubSpot cannot reactivate a user through its API. Reactivate the user in HubSpot, then "
    "run the job again."
)
TEAMS_PATH = f"{USERS_PATH}/teams"
PAGE_SIZE = 100
MAX_PAGES = 1000
INACTIVE_STATES = {
    "archived",
    "deactivated",
    "deleted",
    "disabled",
    "inactive",
    "removed",
    "suspended",
}


def _flag(value) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _is_active(user: dict) -> bool:
    flags = ("archived", "deactivated", "deleted", "inactive", "suspended")
    if any(user.get(flag) is True for flag in flags) or user.get("active") is False:
        return False
    states = (user.get("status"), user.get("state"), user.get("userStatus"))
    return not any(str(state).strip().lower() in INACTIVE_STATES for state in states if state)


class HubSpotAdapter(Adapter):
    slug = "hubspot"
    name = "HubSpot"
    description = (
        "Invites HubSpot users, puts them in teams and removes them when access ends. "
        "Seats and permission sets stay with HubSpot admins."
    )
    supports_groups = True
    group_label = "Teams"
    # Removing the user ends its team memberships too.
    remove_groups_on_disable = False
    fields = (
        Field(
            "access_token",
            "Private app access token",
            kind="secret",
            required=True,
            help=(
                "A HubSpot private app with the scopes settings.users.read, "
                "settings.users.write and settings.users.teams.read."
            ),
        ),
        Field(
            "role_id",
            "Role ID for new users",
            help="Empty: HubSpot's default access. The ID is in Settings > Users & Teams > Roles.",
        ),
        Field(
            "send_welcome_email",
            "Send HubSpot's welcome email",
            kind="bool",
            default=True,
            help="New users get HubSpot's invitation to set their password.",
        ),
        SYNC_INTERVAL_FIELD,
    )

    def __init__(self, config: dict, **kwargs):
        super().__init__(config, **kwargs)
        self._users_route_confirmed = False

    # -- HTTP -------------------------------------------------------------

    def _client(self) -> httpx.Client:
        self.require("access_token")
        return self.http(
            API_URL,
            headers={
                "Authorization": f"Bearer {self.setting('access_token')}",
                "Accept": "application/json",
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
        with self._client() as client:
            try:
                response = client.request(method, url, json=body, params=params)
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

    def _results(self, response: httpx.Response, what: str) -> tuple[list[dict], dict]:
        """The results of a listing and the whole answer. Anything else is an
        error: read as empty, it would mark every account and team missing."""
        try:
            data = response.json()
        except ValueError as exc:
            raise AdapterError(f"HubSpot sent an unreadable answer ({what}).") from exc
        results = data.get("results") if isinstance(data, dict) else None
        if not isinstance(results, list) or not all(isinstance(r, dict) for r in results):
            raise AdapterError(f"HubSpot sent an unexpected answer ({what}).")
        return results, data

    def _require_users_route(self) -> None:
        """A retired API version also answers 404. Only a working users route
        shows that the user is the one missing."""
        if self._users_route_confirmed:
            return
        response = self._send("GET", USERS_PATH, "list users", allow=(404,), params={"limit": "1"})
        if response.status_code == 404:
            raise AdapterError(
                "The HubSpot users API answered 404. Its version may have changed, so no "
                "user can be confirmed missing."
            )
        self._users_route_confirmed = True

    # -- mapping ------------------------------------------------------------

    @staticmethod
    def _teams(user: dict) -> tuple[str, list[str]]:
        primary = user.get("primaryTeamId")
        secondary = [str(t) for t in user.get("secondaryTeamIds") or [] if t not in (None, "")]
        return (str(primary) if primary not in (None, "") else "", secondary)

    def _account(self, user: dict) -> ExternalAccount:
        primary, secondary = self._teams(user)
        teams = {primary, *secondary} - {""}
        name = " ".join(part for part in (user.get("firstName"), user.get("lastName")) if part)
        email = user.get("email") or ""
        return ExternalAccount(
            external_id=str(user.get("id", "")),
            username=email,
            email=email,
            display_name=name,
            active=_is_active(user),
            groups=frozenset(teams),
        )

    def _get_user(self, user_id: str) -> dict | None:
        response = self._send(
            "GET", f"{USERS_PATH}/{quote(user_id, safe='')}", "read a user", allow=(404,)
        )
        if response.status_code == 404:
            return None
        return self._json(response)

    def _get_user_by_email(self, email: str) -> dict | None:
        # safe='' so a '/' in the value cannot reach another path segment.
        response = self._send(
            "GET",
            f"{USERS_PATH}/{quote(email, safe='')}",
            "look up a user",
            allow=(404,),
            params={"idProperty": "EMAIL"},
        )
        if response.status_code == 404:
            self._require_users_route()
            return None
        return self._json(response)

    # -- connection -----------------------------------------------------------

    def test_connection(self) -> TestResult:
        try:
            self._require_users_route()
            self._send("GET", TEAMS_PATH, "list teams")
        except AdapterError as exc:
            return TestResult(False, str(exc))
        return TestResult(True, "Connected to HubSpot. Users and teams can be read.")

    # -- accounts ---------------------------------------------------------------

    def find_account(self, spec: AccountSpec, external_id: str = "") -> ExternalAccount | None:
        if external_id:
            user = self._get_user(external_id)
            if user:
                return self._account(user)
        if not spec.email:
            return None
        user = self._get_user_by_email(spec.email)
        return self._account(user) if user else None

    def create_account(self, spec: AccountSpec) -> EnsureResult:
        if not spec.email:
            raise AdapterError(NEEDS_EMAIL)
        body = {
            "email": spec.email,
            "firstName": spec.first_name,
            "lastName": spec.last_name,
            "sendWelcomeEmail": _flag(self.setting("send_welcome_email", True)),
        }
        if self.setting("role_id"):
            body["roleId"] = str(self.setting("role_id"))
        response = self._send("POST", USERS_PATH, "invite a user", allow=(409,), body=body)
        if response.status_code == 409:
            user = self._get_user_by_email(spec.email)
            if not user:
                raise AdapterError(
                    "HubSpot reports that the user already exists, but no user has this email."
                )
            account = self._account(user)
            if not account.active:
                raise AdapterError(
                    "The HubSpot user exists but is deactivated. Reactivate it in HubSpot, "
                    "then run the job again."
                )
            return EnsureResult(account=account, notes=["The HubSpot user already existed."])
        account = self._account(self._json(response))
        notes = []
        if body["sendWelcomeEmail"]:
            notes.append("HubSpot sent its invitation email.")
        notes.append("Seats and permission sets are assigned in HubSpot, not by this app.")
        return EnsureResult(account=account, created=True, notes=notes)

    def plan_ensure(self, spec: AccountSpec, account: ExternalAccount | None) -> AccountPlan:
        plan = super().plan_ensure(spec, account)
        if plan.change == "create" and not spec.email:
            plan.refusal = NEEDS_EMAIL
        elif plan.change == "reactivate":
            plan.refusal = CANNOT_REACTIVATE
        return plan

    def plan_disable(self, spec: AccountSpec, account: ExternalAccount | None) -> AccountPlan:
        plan = super().plan_disable(spec, account)
        if plan.change == "disable":
            # See set_active: HubSpot has no deactivation in its API.
            plan.change = "delete"
        return plan

    def set_active(self, account: ExternalAccount, active: bool) -> ExternalAccount:
        if active:
            raise AdapterError(CANNOT_REACTIVATE)
        # HubSpot has no deactivation in its API: removing the user ends access.
        # Records the user owns stay in HubSpot and keep the user's name.
        self._send(
            "DELETE",
            f"{USERS_PATH}/{quote(account.external_id, safe='')}",
            "remove a user",
            allow=(404,),
        )
        return replace(account, active=False, groups=frozenset())

    def list_accounts(self) -> Iterable[ExternalAccount]:
        after = None
        for _ in range(MAX_PAGES):
            params = {"limit": str(PAGE_SIZE)}
            if after:
                params["after"] = after
            users, data = self._results(
                self._send("GET", USERS_PATH, "list users", params=params), "list users"
            )
            for user in users:
                yield self._account(user)
            after = ((data.get("paging") or {}).get("next") or {}).get("after")
            if not after:
                return

    # -- teams ----------------------------------------------------------------------

    def list_groups(self) -> list[ExternalGroup]:
        teams, _ = self._results(self._send("GET", TEAMS_PATH, "list teams"), "list teams")
        return [
            ExternalGroup(external_id=str(team["id"]), name=team.get("name") or str(team["id"]))
            for team in teams
            if team.get("id") not in (None, "")
        ]

    def _set_teams(self, account: ExternalAccount, primary: str, secondary: list[str]) -> None:
        self._send(
            "PUT",
            f"{USERS_PATH}/{quote(account.external_id, safe='')}",
            "change a user's teams",
            body={"primaryTeamId": primary or None, "secondaryTeamIds": secondary},
        )
        account.groups = frozenset({primary, *secondary} - {""})

    def _current_teams(self, account: ExternalAccount) -> tuple[str, list[str]]:
        # Read again: each change replaces the user's whole team list.
        user = self._get_user(account.external_id)
        if user is None:
            raise AdapterError("The HubSpot user no longer exists.")
        return self._teams(user)

    def add_to_group(self, account: ExternalAccount, group_id: str) -> None:
        primary, secondary = self._current_teams(account)
        if group_id == primary or group_id in secondary:
            return
        if not primary:
            primary = group_id
        else:
            secondary.append(group_id)
        self._set_teams(account, primary, secondary)

    def remove_from_group(self, account: ExternalAccount, group_id: str) -> None:
        primary, secondary = self._current_teams(account)
        if group_id == primary:
            primary = secondary.pop(0) if secondary else ""
        elif group_id in secondary:
            secondary.remove(group_id)
        else:
            return
        self._set_teams(account, primary, secondary)

    def manual_instructions(self, action: str, spec: AccountSpec, groups: list[str]) -> str:
        who = f"{spec.display_name} ({spec.email or spec.username})"
        if action == "disable_account":
            return (
                f"In HubSpot, reassign the paid seat and owned records of {who}, then remove "
                "or deactivate the user."
            )
        lines = [f"Invite {who} to HubSpot or make sure the user is active."]
        if groups:
            lines.append("Teams: " + ", ".join(groups) + ".")
        lines.append("Assign the seat and permission set the person needs.")
        return "\n".join(lines)

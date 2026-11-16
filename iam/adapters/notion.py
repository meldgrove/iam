"""Notion: changes become tasks; an integration token lets the app read members.

Notion's public API cannot invite or remove workspace members, so this
adapter never writes: every change becomes a manual task. With an internal
integration token that may read user information including email addresses,
the app lists members to compare accounts. Notion does not list removed
members and has no "deactivated" state, so every listed member is active.
"""

from __future__ import annotations

from collections.abc import Iterable

import httpx

from .base import (
    SYNC_INTERVAL_FIELD,
    AccountSpec,
    Adapter,
    AdapterError,
    ConfigurationError,
    ExternalAccount,
    Field,
    TestResult,
    TransientError,
)

BASE_URL = "https://api.notion.com"
NOTION_VERSION = "2022-06-28"
MAX_PAGES = 500


class NotionAdapter(Adapter):
    slug = "notion"
    name = "Notion"
    description = (
        "Tracks Notion workspace members. Invitations and removals become tasks; with "
        "an integration token the app reads members to compare accounts."
    )
    supports_write = False
    supports_groups = False
    fields = (
        Field(
            "token",
            "Integration token",
            kind="secret",
            help=(
                "Optional. An internal integration token with the capability "
                "'Read user information including email addresses'. Without it changes "
                "become tasks and accounts are not compared."
            ),
        ),
        Field(
            "workspace_url",
            "Workspace URL",
            kind="url",
            placeholder="https://www.notion.so/example",
            help="Linked in every task.",
        ),
        SYNC_INTERVAL_FIELD,
    )

    # -- API ---------------------------------------------------------------

    def can_read_accounts(self) -> bool:
        return bool(self.setting("token"))

    def _require_token(self) -> None:
        if not self.can_read_accounts():
            raise ConfigurationError("Notion needs a token to read accounts.")

    def _client(self) -> httpx.Client:
        self._require_token()
        return self.http(
            BASE_URL,
            headers={
                "Authorization": f"Bearer {self.setting('token')}",
                "Notion-Version": NOTION_VERSION,
                "Accept": "application/json",
            },
        )

    def _json(self, response: httpx.Response, what: str) -> dict:
        try:
            data = response.json()
        except ValueError as exc:
            raise AdapterError(f"Notion sent an unexpected answer ({what}).") from exc
        if not isinstance(data, dict):
            raise AdapterError(f"Notion sent an unexpected answer ({what}).")
        return data

    def _get(self, path: str, what: str, params: dict | None = None) -> dict:
        with self._client() as client:
            response = self.call(client, "GET", path, what, params=params or {})
        return self._json(response, what)

    def _get_user(self, user_id: str) -> dict | None:
        """One user by ID, or None when Notion does not know the ID."""
        what = "read a user"
        with self._client() as client:
            try:
                response = client.get(f"/v1/users/{user_id}")
            except httpx.TimeoutException as exc:
                raise TransientError(f"Notion did not answer in time ({what}).") from exc
            except httpx.TransportError as exc:
                raise TransientError(f"Could not reach Notion ({what}).") from exc
        # 400: not a valid user ID; 404: unknown or no longer a member.
        if response.status_code in (400, 404):
            return None
        self.raise_for_status(response, what)
        return self._json(response, what)

    def _users(self) -> Iterable[dict]:
        cursor = ""
        seen: set[str] = set()
        for _ in range(MAX_PAGES):
            params: dict = {"page_size": 100}
            if cursor:
                params["start_cursor"] = cursor
            data = self._get("/v1/users", "list users", params)
            results = data.get("results")
            if not isinstance(results, list) or not isinstance(data.get("has_more"), bool):
                raise AdapterError("Notion sent an unexpected answer (list users).")
            yield from (user for user in results if isinstance(user, dict))
            if not data["has_more"]:
                return
            cursor = str(data.get("next_cursor") or "")
            if not cursor or cursor in seen:
                raise AdapterError("Notion sent inconsistent pages (list users).")
            seen.add(cursor)
        raise AdapterError(f"Notion listed more than {MAX_PAGES} pages of users.")

    @staticmethod
    def _email(user: dict) -> str:
        person = user.get("person") if isinstance(user.get("person"), dict) else {}
        return str(person.get("email") or "")

    @classmethod
    def _account(cls, user: dict) -> ExternalAccount:
        email = cls._email(user)
        return ExternalAccount(
            external_id=str(user.get("id") or ""),
            username=email,
            email=email,
            display_name=str(user.get("name") or ""),
            active=True,
        )

    # -- adapter -------------------------------------------------------------

    def test_connection(self) -> TestResult:
        if not self.can_read_accounts():
            return TestResult(
                True, "No token: changes become manual tasks and accounts are not compared."
            )
        data = self._get("/v1/users/me", "read the integration")
        bot = data.get("bot") if isinstance(data.get("bot"), dict) else {}
        workspace = str(bot.get("workspace_name") or "")
        return TestResult(
            True,
            f"Connected to Notion workspace {workspace}." if workspace else "Connected to Notion.",
        )

    def find_account(self, spec: AccountSpec, external_id: str = "") -> ExternalAccount | None:
        self._require_token()
        if external_id:
            user = self._get_user(external_id)
            if user and user.get("type") == "person":
                return self._account(user)
        if not spec.email:
            return None
        wanted = spec.email.lower()
        hidden = False
        for user in self._users():
            if user.get("type") != "person":
                continue
            email = self._email(user)
            if not email:
                hidden = True
            elif email.lower() == wanted:
                return self._account(user)
        if hidden:
            # A member without a visible email could be this person.
            raise AdapterError(
                "Notion hides the email address of some members, so this account "
                "cannot be found. Give the integration the capability 'Read user "
                "information including email addresses'."
            )
        return None

    def list_accounts(self) -> Iterable[ExternalAccount]:
        self._require_token()
        for user in self._users():
            if user.get("type") == "person" and user.get("id"):
                yield self._account(user)

    def manual_instructions(self, action: str, spec: AccountSpec, groups: list[str]) -> str:
        who = f"{spec.display_name} ({spec.email or spec.username})"
        url = self.setting("workspace_url")
        if action == "disable_account":
            lines = [
                f"Remove {who} from the Notion workspace: Settings > People > "
                "Remove from workspace.",
                "Transfer their private pages first if the team still needs them.",
            ]
        else:
            lines = [
                f"Invite {who} to the Notion workspace as a member with "
                f"{spec.email or 'their work email'}.",
                "Add them to the teamspaces their team uses.",
            ]
        if url:
            lines.append(f"Workspace: {url}")
        if not self.can_read_accounts():
            lines.append("The app cannot read Notion, so completing this task records the change.")
        return "\n".join(lines)

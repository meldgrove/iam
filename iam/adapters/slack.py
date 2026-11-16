"""Slack: changes become tasks; a bot token lets the app read accounts.

Slack's APIs for creating and deactivating members need Enterprise Grid (or
SCIM on Business+), so this adapter never writes: every account or channel
change becomes a manual task with the steps below. With a bot token
(scopes `users:read`, `users:read.email`, `channels:read`, `groups:read`) the
app reads members and channels, so it can compare accounts and import
channels as groups.

Channel membership is not read: that needs one `conversations.members` call
per channel. Accounts therefore carry `groups=None` (not observed), and
`account_groups` returns an empty set only because ensure/disable never run
for an adapter without writes; drift comparison skips channels.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

from .base import (
    SYNC_INTERVAL_FIELD,
    AccountSpec,
    Adapter,
    AdapterError,
    ConfigurationError,
    ExternalAccount,
    ExternalGroup,
    Field,
    TestResult,
    TransientError,
)

BASE_URL = "https://slack.com/api"
MAX_PAGES = 500

CONFIGURATION_ERRORS = {
    "account_inactive",
    "invalid_auth",
    "missing_scope",
    "no_permission",
    "not_allowed_token_type",
    "not_authed",
    "team_access_not_granted",
    "token_expired",
    "token_revoked",
}
NOT_FOUND_ERRORS = {"users_not_found", "user_not_found"}


class SlackApiError(AdapterError):
    def __init__(self, message: str, code: str):
        super().__init__(message)
        self.code = code


class SlackAdapter(Adapter):
    slug = "slack"
    name = "Slack"
    description = (
        "Tracks Slack members. Invitations, channels and deactivations become tasks; "
        "with a bot token the app reads members and channels to compare accounts."
    )
    supports_write = False
    supports_groups = True
    # Without a token channels cannot be imported, so admins may add them by hand.
    manual_groups = True
    group_label = "Channels"
    fields = (
        Field(
            "bot_token",
            "Bot token",
            kind="secret",
            help=(
                "Optional. A bot token (xoxb-...) with users:read, users:read.email, "
                "channels:read and groups:read. Without it changes become tasks and "
                "accounts are not compared."
            ),
        ),
        Field(
            "workspace_url",
            "Workspace URL",
            kind="url",
            placeholder="https://example.slack.com",
            help="Linked in every task.",
        ),
        Field(
            "guest_types",
            "Invite as guests",
            default="external",
            help=(
                "Person types invited as guests, comma-separated (employee, intern, "
                "freelancer, volunteer, external). Everyone else becomes a full member."
            ),
        ),
        SYNC_INTERVAL_FIELD,
    )

    # -- API ---------------------------------------------------------------

    def can_read_accounts(self) -> bool:
        return bool(self.setting("bot_token"))

    def _require_token(self) -> None:
        if not self.can_read_accounts():
            raise ConfigurationError("Slack needs a bot token to read accounts.")

    def _api(self, method: str, params: dict | None = None) -> dict:
        self._require_token()
        with self.http(
            BASE_URL, headers={"Authorization": f"Bearer {self.setting('bot_token')}"}
        ) as client:
            response = self.call(client, "GET", f"/{method}", f"call {method}", params=params or {})
        try:
            data = response.json()
        except ValueError as exc:
            raise AdapterError(f"Slack sent an unexpected answer to {method}.") from exc
        if not isinstance(data, dict) or not isinstance(data.get("ok"), bool):
            raise AdapterError(f"Slack sent an unexpected answer to {method}.")
        if data["ok"]:
            return data
        code = str(data.get("error") or "unknown_error")
        # Error codes are fixed identifiers; keep only those characters.
        code = re.sub(r"[^a-z0-9_]", "", code.lower())[:60] or "unknown_error"
        if code == "ratelimited":
            raise TransientError(f"Slack rate-limited {method}; it runs again later.")
        if code in CONFIGURATION_ERRORS:
            raise ConfigurationError(
                f"Slack refused {method} ({code}). Check the bot token and its scopes."
            )
        raise SlackApiError(f"Slack answered {method} with {code}.", code)

    def _paged(self, method: str, key: str, params: dict) -> Iterable[dict]:
        cursor = ""
        seen: set[str] = set()
        for _ in range(MAX_PAGES):
            page_params = dict(params)
            if cursor:
                page_params["cursor"] = cursor
            data = self._api(method, page_params)
            items = data.get(key)
            if not isinstance(items, list):
                raise AdapterError(f"Slack sent an unexpected answer to {method}.")
            yield from (item for item in items if isinstance(item, dict))
            metadata = data.get("response_metadata") or {}
            cursor = str(metadata.get("next_cursor") or "") if isinstance(metadata, dict) else ""
            if not cursor:
                return
            if cursor in seen:
                raise AdapterError(f"Slack repeated a page of {method}.")
            seen.add(cursor)
        raise AdapterError(f"Slack listed more than {MAX_PAGES} pages of {method}.")

    @staticmethod
    def _account(user: dict) -> ExternalAccount:
        profile = user.get("profile") if isinstance(user.get("profile"), dict) else {}
        return ExternalAccount(
            external_id=str(user.get("id") or ""),
            username=str(user.get("name") or ""),
            email=str(profile.get("email") or ""),
            display_name=str(profile.get("real_name") or user.get("real_name") or ""),
            active=not bool(user.get("deleted")),
            groups=None,
        )

    @staticmethod
    def _is_person(user: dict) -> bool:
        return not (user.get("is_bot") or user.get("is_app_user") or user.get("id") == "USLACKBOT")

    # -- adapter -------------------------------------------------------------

    def test_connection(self) -> TestResult:
        if not self.can_read_accounts():
            return TestResult(
                True, "No token: changes become manual tasks and accounts are not compared."
            )
        data = self._api("auth.test")
        team = str(data.get("team") or data.get("team_id") or "the workspace")
        return TestResult(True, f"Connected to Slack workspace {team}.")

    def list_groups(self) -> list[ExternalGroup]:
        if not self.can_read_accounts():
            raise ConfigurationError(
                "Slack needs a bot token to import channels. Add channels by hand instead."
            )
        groups = []
        for channel in self._paged(
            "conversations.list",
            "channels",
            {"types": "public_channel,private_channel", "exclude_archived": "true", "limit": 200},
        ):
            if channel.get("is_archived") or not channel.get("id"):
                continue
            purpose = channel.get("purpose") if isinstance(channel.get("purpose"), dict) else {}
            groups.append(
                ExternalGroup(
                    external_id=str(channel["id"]),
                    name=f"#{channel.get('name') or channel['id']}",
                    description=str(purpose.get("value") or ""),
                )
            )
        return groups

    def find_account(self, spec: AccountSpec, external_id: str = "") -> ExternalAccount | None:
        self._require_token()
        if external_id:
            try:
                data = self._api("users.info", {"user": external_id})
            except SlackApiError as exc:
                if exc.code not in NOT_FOUND_ERRORS:
                    raise
            else:
                user = data.get("user")
                if isinstance(user, dict) and user.get("id"):
                    return self._account(user)
        if not spec.email:
            return None
        try:
            data = self._api("users.lookupByEmail", {"email": spec.email})
        except SlackApiError as exc:
            # Also the answer for deactivated members and pending invitations.
            if exc.code in NOT_FOUND_ERRORS:
                return None
            raise
        user = data.get("user")
        if not isinstance(user, dict) or not user.get("id"):
            raise AdapterError("Slack sent an unexpected answer to users.lookupByEmail.")
        return self._account(user)

    def account_groups(self, account: ExternalAccount) -> set[str]:
        # Channel membership is not read (see module docstring).
        return set()

    def list_accounts(self) -> Iterable[ExternalAccount]:
        self._require_token()
        for user in self._paged("users.list", "members", {"limit": 200}):
            if self._is_person(user) and user.get("id"):
                yield self._account(user)

    def _invites_as_guest(self, person_type: str) -> bool:
        types = {
            item.strip().lower()
            for item in str(self.setting("guest_types", "")).split(",")
            if item.strip()
        }
        return bool(person_type) and person_type.lower() in types

    def manual_instructions(self, action: str, spec: AccountSpec, groups: list[str]) -> str:
        who = f"{spec.display_name} ({spec.email or spec.username})"
        url = self.setting("workspace_url")
        if action == "disable_account":
            lines = [
                f"Deactivate the Slack account of {who}: Admin > Manage members > "
                "Deactivate account. Their messages and files stay in Slack.",
            ]
        else:
            role = (
                "a guest (multi-channel guest)"
                if self._invites_as_guest(spec.person_type)
                else "a full member"
            )
            lines = [
                f"Invite {who} to the Slack workspace as {role} with {spec.email or 'their work email'}.",
                "If they already have a deactivated account, reactivate it instead.",
            ]
            if groups:
                lines.append("Add them to these channels: " + ", ".join(groups) + ".")
        if url:
            lines.append(f"Workspace: {url}")
        if not self.can_read_accounts():
            lines.append("The app cannot read Slack, so completing this task records the change.")
        return "\n".join(lines)

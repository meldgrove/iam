"""Keycloak: users and groups in one realm, through the Admin REST API.

The connection signs in as a confidential client with a service account
(client credentials grant). Give that service account these roles of the
realm's `realm-management` client: view-users, query-users, manage-users,
query-groups and view-realm.
"""

from __future__ import annotations

import time
from collections.abc import Iterable, Iterator
from urllib.parse import quote, urlsplit

import httpx
from django.conf import settings

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
    generate_password,
)

PAGE_SIZE = 100
REQUIRED_ROLES = "view-users, query-users, manage-users, query-groups and view-realm"
LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1"}


def path_segment(value: str) -> str:
    """One URL path segment for a user or group ID. "." and ".." would be
    resolved as relative steps out of the users path, so they are refused."""
    value = str(value or "")
    if value in {"", ".", ".."}:
        raise AdapterError("Keycloak returned an identifier that is not a valid path segment.")
    return quote(value, safe="")


class KeycloakAdapter(Adapter):
    slug = "keycloak"
    name = "Keycloak"
    description = (
        "Creates, enables and disables users in a Keycloak realm and manages their group "
        "memberships. Signs in as a confidential client with a service account."
    )
    supports_groups = True
    group_label = "Groups"
    fields = (
        Field(
            "base_url",
            "Keycloak URL",
            kind="url",
            required=True,
            destination=True,
            placeholder="https://sso.example.invalid",
            help="Without /realms/... Include /auth only for Keycloak versions before 17.",
        ),
        Field(
            "allow_http",
            "Allow plain HTTP",
            kind="bool",
            default=False,
            destination=True,
            help=(
                "Only for a Keycloak in the same private network, e.g. http://keycloak:8080 "
                "inside one Docker network. Everything else must use HTTPS."
            ),
        ),
        Field("realm", "Realm", required=True, placeholder="example"),
        Field(
            "client_id",
            "Client ID",
            required=True,
            help=(
                "A confidential client with service accounts on. Its service account needs "
                f"the realm-management roles {REQUIRED_ROLES}."
            ),
        ),
        Field("client_secret", "Client secret", kind="secret", required=True),
        Field(
            "group_root",
            "Group root",
            placeholder="/apps",
            help="Import only the groups below this group path. Empty imports every group.",
        ),
        Field(
            "username_source",
            "Username",
            kind="choice",
            default="username",
            choices=(
                ("username", "Login name (the work email before @)"),
                ("email", "Full work email"),
            ),
        ),
        Field(
            "send_setup_email",
            "Send setup email",
            kind="bool",
            default=True,
            help=(
                "After creating a user, Keycloak emails a link to set a password to the "
                "user's email. Needs email settings in the realm."
            ),
        ),
        Field(
            "setup_email_lifespan_hours",
            "Setup link valid for (hours)",
            kind="int",
            default=72,
        ),
        Field(
            "set_temporary_password",
            "Set a temporary password",
            kind="bool",
            default=False,
            help=(
                "Instead of the setup email: set a temporary password, which the app sends "
                "to the person's private email through the email connection. Keycloak asks "
                "for a new password at the first sign-in."
            ),
        ),
        Field(
            "logout_on_disable",
            "End sessions on disable",
            kind="bool",
            default=True,
            help="Sign the user out of every session when the account is disabled.",
        ),
        SYNC_INTERVAL_FIELD,
    )

    def __init__(self, config: dict, **kwargs):
        super().__init__(config, **kwargs)
        self._client_instance: httpx.Client | None = None
        self._token = ""
        self._token_expires_at = 0.0
        # Full user representations from the last reads, by user ID.
        self._users: dict[str, dict] = {}

    # -- configuration ---------------------------------------------------------

    @property
    def base_url(self) -> str:
        return str(self.setting("base_url", "")).rstrip("/")

    @property
    def realm(self) -> str:
        return str(self.setting("realm", "")).strip()

    def _check_config(self) -> None:
        self.require("base_url", "realm", "client_id", "client_secret")
        parts = urlsplit(self.base_url)
        if parts.scheme not in {"http", "https"} or not parts.hostname:
            raise ConfigurationError("The Keycloak URL must start with https://.")
        if parts.scheme != "https" and not (
            settings.DEBUG or parts.hostname in LOOPBACK_HOSTS or self.setting("allow_http")
        ):
            raise ConfigurationError(
                "The Keycloak URL must use HTTPS: the client secret and user data travel "
                "over this connection."
            )
        if parts.query or parts.fragment:
            raise ConfigurationError("The Keycloak URL must not contain ? or #.")

    def _client(self) -> httpx.Client:
        if self._client_instance is None:
            self._check_config()
            self._client_instance = self.http(base_url=f"{self.base_url}/")
        return self._client_instance

    def _admin_path(self, path: str = "") -> str:
        root = f"admin/realms/{path_segment(self.realm)}"
        return f"{root}/{path}" if path else root

    def _user_path(self, user_id: str, suffix: str = "") -> str:
        path = self._admin_path(f"users/{path_segment(user_id)}")
        return f"{path}/{suffix}" if suffix else path

    # -- HTTP --------------------------------------------------------------------

    def _send(self, method: str, path: str, what: str, **kwargs) -> httpx.Response:
        try:
            return self._client().request(method, path, **kwargs)
        except httpx.TimeoutException as exc:
            raise TransientError(f"Keycloak did not answer in time ({what}).") from exc
        except httpx.TransportError as exc:
            raise TransientError(f"Could not reach Keycloak ({what}).") from exc

    def _access_token(self) -> str:
        if self._token and time.monotonic() < self._token_expires_at:
            return self._token
        what = "sign in with the client credentials"
        response = self._send(
            "POST",
            f"realms/{path_segment(self.realm)}/protocol/openid-connect/token",
            what,
            data={
                "grant_type": "client_credentials",
                "client_id": self.setting("client_id"),
                "client_secret": self.setting("client_secret"),
            },
        )
        if response.status_code in (400, 401):
            # A wrong secret answers 401; a client without service accounts 400.
            raise ConfigurationError(
                f"Keycloak refused the client ID and secret ({response.status_code}). Check "
                "the secret and that the client has client authentication and service "
                "accounts turned on."
            )
        if response.status_code == 404:
            raise ConfigurationError(
                f"Keycloak has no realm {self.realm!r} at this URL (404). Check the URL and realm."
            )
        self.raise_for_status(response, what)
        data = self._json(response, what)
        token = data.get("access_token") if isinstance(data, dict) else None
        if not token:
            raise AdapterError("Keycloak's token answer contained no access token.")
        try:
            expires_in = float(data.get("expires_in"))
        except (TypeError, ValueError):
            expires_in = 60.0
        self._token = str(token)
        # Refresh before the token ends: one adapter can make many calls.
        self._token_expires_at = time.monotonic() + max(expires_in - 30.0, 10.0)
        return self._token

    def _request(
        self, method: str, path: str, what: str, *, missing_ok: bool = False, **kwargs
    ) -> httpx.Response | None:
        retried = False
        while True:
            fresh = not self._token or time.monotonic() >= self._token_expires_at
            headers = {"Authorization": f"Bearer {self._access_token()}"}
            response = self._send(method, path, what, headers=headers, **kwargs)
            if response.status_code == 401 and not fresh and not retried:
                # The cached token was revoked or expired early: sign in again once.
                self._token = ""
                retried = True
                continue
            break
        if response.status_code == 404 and missing_ok:
            return None
        if response.status_code == 403:
            raise ConfigurationError(
                f"Keycloak refused access (403) while trying to {what}. Give the client's "
                f"service account the realm-management roles {REQUIRED_ROLES}."
            )
        self.raise_for_status(response, what)
        return response

    def _json(self, response: httpx.Response, what: str):
        try:
            return response.json()
        except ValueError as exc:
            raise AdapterError(
                f"Keycloak sent an unreadable answer while trying to {what}."
            ) from exc

    def _get_list(self, path: str, what: str, **params) -> list[dict]:
        response = self._request("GET", path, what, params=params)
        data = self._json(response, what)
        if not isinstance(data, list):
            raise AdapterError(f"Keycloak sent an unexpected answer while trying to {what}.")
        return [item for item in data if isinstance(item, dict)]

    def _pages(self, path: str, what: str, **params) -> Iterator[dict]:
        first = 0
        while True:
            page = self._get_list(path, what, first=first, max=PAGE_SIZE, **params)
            yield from page
            if len(page) < PAGE_SIZE:
                return
            first += PAGE_SIZE

    # -- conversions --------------------------------------------------------------

    def _remember(self, user: dict) -> ExternalAccount:
        user_id = str(user.get("id") or "")
        if not user_id:
            raise AdapterError("Keycloak returned a user without an ID.")
        self._users[user_id] = user
        return ExternalAccount(
            external_id=user_id,
            username=str(user.get("username") or ""),
            email=str(user.get("email") or ""),
            display_name=" ".join(
                part for part in (user.get("firstName"), user.get("lastName")) if part
            ),
            active=bool(user.get("enabled", False)),
            groups=None,
        )

    def _username(self, spec: AccountSpec) -> str:
        if self.setting("username_source") == "email" and spec.email:
            return spec.email.strip().lower()
        username = spec.username or (spec.email.split("@", 1)[0] if spec.email else "")
        username = username.strip().lower()
        if not username:
            raise AdapterError("The person has neither a login name nor a work email.")
        return username

    # -- checks and catalog ---------------------------------------------------------

    def test_connection(self) -> TestResult:
        try:
            self._access_token()
            response = self._request("GET", self._admin_path("users/count"), "count users")
            count = self._json(response, "count users")
            self._get_list(self._admin_path("groups"), "list groups", first=0, max=1)
        except AdapterError as exc:
            return TestResult(False, str(exc))
        return TestResult(True, f"Connected to realm {self.realm}: {count} users.")

    def list_groups(self) -> list[ExternalGroup]:
        root = str(self.setting("group_root", "")).strip().rstrip("/")
        if root and not root.startswith("/"):
            root = f"/{root}"
        seen: set[str] = set()
        groups: list[ExternalGroup] = []
        for top in self._pages(
            self._admin_path("groups"), "list groups", briefRepresentation="false"
        ):
            groups.extend(self._walk_group(top, "", seen))
        if root:
            groups = [group for group in groups if group.path.startswith(f"{root}/")]
        return groups

    def _walk_group(self, group: dict, parent_path: str, seen: set[str]) -> Iterator[ExternalGroup]:
        group_id = str(group.get("id") or "").strip()
        if not group_id or group_id in seen:
            return
        seen.add(group_id)
        name = str(group.get("name") or "").strip()
        path = str(group.get("path") or "").strip()
        if not path and name:
            path = f"{parent_path}/{name}"
        if not path:
            return
        attributes = group.get("attributes")
        description = attributes.get("description") if isinstance(attributes, dict) else None
        yield ExternalGroup(
            external_id=group_id,
            name=name or path.rsplit("/", 1)[-1],
            path=path,
            description=str(description[0])
            if isinstance(description, list) and description
            else "",
        )
        count = group.get("subGroupCount")
        inline = group.get("subGroups")
        children: Iterable[dict]
        if isinstance(count, int) and not isinstance(count, bool):
            # Keycloak 23+ leaves subGroups empty in listings and counts them instead.
            if count == 0:
                return
            children = self._pages(
                self._admin_path(f"groups/{path_segment(group_id)}/children"),
                f"list the subgroups of {path}",
                briefRepresentation="false",
            )
        elif isinstance(inline, list):
            # Older versions return the whole tree in the listing.
            children = [child for child in inline if isinstance(child, dict)]
        else:
            children = []
        for child in children:
            yield from self._walk_group(child, path, seen)

    # -- accounts --------------------------------------------------------------------

    def _get_user(self, user_id: str) -> dict | None:
        response = self._request("GET", self._user_path(user_id), "read a user", missing_ok=True)
        if response is None:
            return None
        user = self._json(response, "read a user")
        if not isinstance(user, dict) or str(user.get("id") or "") != user_id:
            raise AdapterError("Keycloak sent an unexpected answer while trying to read a user.")
        return user

    def _search(self, field: str, value: str) -> dict | None:
        what = f"search users by {field}"
        users = self._get_list(
            self._admin_path("users"),
            what,
            **{field: value, "exact": "true", "briefRepresentation": "false"},
        )
        matches = [
            user
            for user in users
            if str(user.get(field) or "").strip().lower() == value.strip().lower()
        ]
        if len(matches) > 1:
            raise AdapterError(f"Several Keycloak users have the {field} {value}.")
        return matches[0] if matches else None

    def find_account(self, spec: AccountSpec, external_id: str = "") -> ExternalAccount | None:
        if external_id:
            user = self._get_user(external_id)
            if user is not None:
                return self._remember(user)
        user = self._search("username", self._username(spec))
        if user is None and spec.email:
            user = self._search("email", spec.email)
        return self._remember(user) if user is not None else None

    def create_account(self, spec: AccountSpec) -> EnsureResult:
        username = self._username(spec)
        payload = {
            "username": username,
            "firstName": spec.first_name,
            "lastName": spec.last_name,
            "enabled": True,
            "emailVerified": False,
        }
        if spec.email:
            payload["email"] = spec.email.strip().lower()
        response = self._post_user(payload)
        if response is None:
            return self._adopt_existing(spec, username)
        user_id = response.headers.get("Location", "").rstrip("/").rsplit("/users/", 1)[-1]
        user = self._get_user(user_id) if user_id and "/" not in user_id else None
        if user is None:
            user = self._search("username", username)
        if user is None:
            raise TransientError("Keycloak created the user but does not list it yet.")
        account = self._remember(user)
        account.groups = frozenset()
        result = EnsureResult(account=account, created=True)
        self._deliver_credentials(spec, account, result)
        return result

    def _post_user(self, payload: dict) -> httpx.Response | None:
        """Create a user; None when Keycloak already has one (409)."""
        what = "create a user"
        headers = {"Authorization": f"Bearer {self._access_token()}"}
        response = self._send(
            "POST", self._admin_path("users"), what, headers=headers, json=payload
        )
        if response.status_code == 409:
            return None
        if response.status_code == 401:
            self._token = ""
            return self._request("POST", self._admin_path("users"), what, json=payload)
        if response.status_code == 403:
            raise ConfigurationError(
                "Keycloak refused access (403) while trying to create a user. Give the "
                f"client's service account the realm-management roles {REQUIRED_ROLES}."
            )
        self.raise_for_status(response, what)
        return response

    def _adopt_existing(self, spec: AccountSpec, username: str) -> EnsureResult:
        """A create conflicted, e.g. after a lost answer: link the user that
        exists when it is plausibly the same person."""
        user = self._search("username", username)
        if user is None and spec.email:
            user = self._search("email", spec.email)
        if user is None:
            raise AdapterError(
                "Keycloak reports that the user exists but finds no user with this "
                "username or email."
            )
        existing_email = str(user.get("email") or "").strip().lower()
        if existing_email and spec.email and existing_email != spec.email.strip().lower():
            raise AdapterError(
                f"The Keycloak username {username} belongs to a user with a different email."
            )
        account = self._remember(user)
        result = EnsureResult(account=account, notes=["Keycloak already had this user."])
        if not account.active:
            result.account = self.set_active(account, True)
            result.reactivated = True
        return result

    def _deliver_credentials(
        self, spec: AccountSpec, account: ExternalAccount, result: EnsureResult
    ) -> None:
        """Arrange the first sign-in. Failures only add a note: the user exists,
        and a retry would find it and not come back here."""
        if self.setting("send_setup_email") and spec.email:
            hours = int(self.setting("setup_email_lifespan_hours", 72) or 72)
            response = self._send(
                "PUT",
                self._user_path(account.external_id, "execute-actions-email"),
                "send the setup email",
                headers={"Authorization": f"Bearer {self._access_token()}"},
                params={"lifespan": max(hours, 1) * 3600},
                json=["UPDATE_PASSWORD"],
            )
            if response.status_code < 400:
                result.notes.append(f"Keycloak sent the setup email to {spec.email}.")
                return
            result.notes.append(
                f"Keycloak could not send the setup email ({response.status_code}). Check the "
                "realm's email settings, then send it from the Keycloak admin console: "
                "Users > the user > Credentials > Credential reset."
            )
            return
        if self.setting("set_temporary_password"):
            password = generate_password()
            response = self._send(
                "PUT",
                self._user_path(account.external_id, "reset-password"),
                "set a temporary password",
                headers={"Authorization": f"Bearer {self._access_token()}"},
                json={"type": "password", "value": password, "temporary": True},
            )
            if response.status_code < 400:
                result.initial_password = password
                return
            result.notes.append(
                f"Keycloak refused the temporary password ({response.status_code}), for "
                "example because of the realm's password policy. Set one in the Keycloak "
                "admin console."
            )

    def set_active(self, account: ExternalAccount, active: bool) -> ExternalAccount:
        what = "enable a user" if active else "disable a user"
        # Send the whole representation back: partial updates can drop
        # attributes under Keycloak's declarative user profile.
        user = self._get_user(account.external_id)
        if user is None:
            raise AdapterError("The Keycloak user no longer exists.")
        user = {key: value for key, value in user.items() if key != "access"}
        user["enabled"] = active
        self._request("PUT", self._user_path(account.external_id), what, json=user)
        if not active and self.setting("logout_on_disable"):
            self._request(
                "POST",
                self._user_path(account.external_id, "logout"),
                "end the user's sessions",
            )
        updated = self._remember(user)
        updated.groups = account.groups
        return updated

    def account_groups(self, account: ExternalAccount) -> set[str]:
        if account.groups is not None:
            return set(account.groups)
        groups = {
            str(group["id"])
            for group in self._pages(
                self._user_path(account.external_id, "groups"),
                "read a user's groups",
                briefRepresentation="true",
            )
            if group.get("id")
        }
        account.groups = frozenset(groups)
        return groups

    def add_to_group(self, account: ExternalAccount, group_id: str) -> None:
        self._request(
            "PUT",
            self._user_path(account.external_id, f"groups/{path_segment(group_id)}"),
            f"add a user to group {group_id}",
        )

    def remove_from_group(self, account: ExternalAccount, group_id: str) -> None:
        # 404: the group is gone, so the membership is too.
        self._request(
            "DELETE",
            self._user_path(account.external_id, f"groups/{path_segment(group_id)}"),
            f"remove a user from group {group_id}",
            missing_ok=True,
        )

    def ensure_account(
        self,
        spec: AccountSpec,
        *,
        external_id: str = "",
        groups: set[str],
        managed_groups: set[str],
    ) -> EnsureResult:
        result = super().ensure_account(
            spec, external_id=external_id, groups=groups, managed_groups=managed_groups
        )
        if not result.created:
            self._update_names(spec, result)
        return result

    def _update_names(self, spec: AccountSpec, result: EnsureResult) -> None:
        """Keep first and last name current. Username and email stay as they
        are: changing them can hand a login to someone else, so that is a
        decision for a Keycloak admin."""
        user = self._users.get(result.account.external_id)
        if user is None:
            return
        wanted = {"firstName": spec.first_name, "lastName": spec.last_name}
        if any((user.get(key) or "") != value for key, value in wanted.items()):
            current = self._get_user(result.account.external_id)
            if current is not None:
                current = {key: value for key, value in current.items() if key != "access"}
                current.update(wanted)
                self._request(
                    "PUT",
                    self._user_path(result.account.external_id),
                    "update a user's name",
                    json=current,
                )
                result.notes.append("Updated the name in Keycloak.")
        if spec.email and (user.get("email") or "").lower() != spec.email.strip().lower():
            result.notes.append(
                "The email in Keycloak differs from the work email; the app left it unchanged."
            )

    def list_accounts(self) -> Iterable[ExternalAccount]:
        for user in self._pages(
            self._admin_path("users"), "list users", briefRepresentation="false"
        ):
            if user.get("serviceAccountClientLink"):
                continue
            yield self._remember(user)

    def manual_instructions(self, action: str, spec: AccountSpec, groups: list[str]) -> str:
        who = f"{spec.display_name} ({spec.email or spec.username})"
        console = f"the Keycloak admin console, realm {self.realm or '(not set)'}"
        if action == "disable_account":
            return (
                f"In {console}, open Users, find {who}, turn Enabled off and sign the user "
                "out of all sessions. Remove the groups this app manages."
            )
        lines = [
            f"In {console}, make sure {who} exists under Users and is enabled.",
        ]
        if groups:
            lines.append("Groups: " + ", ".join(groups) + ".")
        return "\n".join(lines)

"""Google Workspace accounts and Google Groups through the Admin SDK
Directory API, as a service account with domain-wide delegation."""

from __future__ import annotations

import json
from collections.abc import Iterable, Iterator
from urllib.parse import quote

import httpx

from .base import (
    SYNC_INTERVAL_FIELD,
    AccountPlan,
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

# 403 reasons of quotas and rate limits: the request may run again later.
RATE_LIMIT_REASONS = {"userRateLimitExceeded", "rateLimitExceeded", "quotaExceeded"}
API_BASE = "https://admin.googleapis.com"
DIRECTORY = "/admin/directory/v1"
SCOPES = (
    "https://www.googleapis.com/auth/admin.directory.user",
    "https://www.googleapis.com/auth/admin.directory.group",
    "https://www.googleapis.com/auth/admin.directory.group.member",
)


def _error_reasons(response: httpx.Response) -> set[str]:
    """The reasons of a Google API error answer; never shown to anyone."""
    try:
        error = response.json().get("error")
        return {str(e.get("reason")) for e in error.get("errors") or [] if isinstance(e, dict)}
    except (ValueError, AttributeError, TypeError):
        return set()


def _lacks_admin_privileges(user: dict) -> bool:
    """Missing flags mean unknown privileges, not a normal user."""
    return all(user.get(flag) is False for flag in ("isAdmin", "isDelegatedAdmin"))


def _refusal(user: dict, active: bool) -> str:
    """Why the app does not reactivate or suspend this user, if it does not."""
    if active and not _lacks_admin_privileges(user):
        return (
            "Suspended Google Workspace administrator accounts are not reactivated "
            "automatically. Reactivate the account in the Google Admin console."
        )
    if not active and user.get("isAdmin") is True:
        return "Google Workspace super admins are not suspended automatically."
    return ""


def _alias_refusal(email: str) -> str:
    return (
        f"{email} is an alias of another Google Workspace user. Nothing was changed. Remove "
        "the alias in the Google Admin console or use another work email."
    )


class GoogleWorkspaceAdapter(Adapter):
    slug = "google_workspace"
    name = "Google Workspace"
    description = (
        "Creates, suspends and reactivates Google Workspace users and manages their "
        "Google Groups memberships."
    )
    supports_groups = True
    group_label = "Google Groups"
    fields = (
        Field(
            "service_account_json",
            "Service account key (JSON)",
            kind="secret_text",
            required=True,
            help=(
                "The JSON key of a service account with domain-wide delegation for the "
                "scopes admin.directory.user, admin.directory.group and "
                "admin.directory.group.member."
            ),
        ),
        Field(
            "admin_email",
            "Admin user",
            kind="email",
            required=True,
            help="The Google Workspace admin the service account acts as.",
            placeholder="admin@example.invalid",
        ),
        Field(
            "domain",
            "Primary domain",
            required=True,
            help="Work emails must be in this domain.",
            placeholder="example.invalid",
        ),
        Field(
            "customer_id",
            "Customer ID",
            default="my_customer",
            help="Leave as my_customer unless the admin user manages several accounts.",
        ),
        Field(
            "org_unit_path",
            "Organizational unit for new users",
            default="/",
            placeholder="/Staff",
        ),
        Field(
            "deliver_initial_password",
            "Email a first password",
            kind="bool",
            default=True,
            help=(
                "New users get a generated password that must be changed at first sign-in. "
                "The app emails it to the person's personal address through the email "
                "connection and never stores it."
            ),
        ),
        SYNC_INTERVAL_FIELD,
    )

    # -- connection ------------------------------------------------------------

    @property
    def domain(self) -> str:
        return str(self.setting("domain", "")).strip().lstrip("@").lower()

    @property
    def customer(self) -> str:
        return self.setting("customer_id", "my_customer")

    def _access_token(self) -> str:
        self.require("service_account_json", "admin_email", "domain")
        from google.auth.exceptions import GoogleAuthError, RefreshError, TransportError
        from google.auth.transport.requests import Request
        from google.oauth2 import service_account

        raw = self.setting("service_account_json")
        try:
            info = json.loads(raw) if isinstance(raw, str) else dict(raw)
            credentials = service_account.Credentials.from_service_account_info(
                info, scopes=list(SCOPES), subject=self.setting("admin_email")
            )
        except (ValueError, TypeError, KeyError) as exc:
            raise ConfigurationError(
                "The service account key is not a valid Google service account JSON key."
            ) from exc
        try:
            credentials.refresh(Request())
        except RefreshError as exc:
            raise ConfigurationError(
                "Google refused the service account. Check domain-wide delegation for its "
                "client ID, the scopes admin.directory.user, admin.directory.group and "
                "admin.directory.group.member, and that the admin user is an administrator."
            ) from exc
        except TransportError as exc:
            raise TransientError("Could not reach Google to sign in the service account.") from exc
        except GoogleAuthError as exc:
            raise ConfigurationError("Google could not sign in the service account.") from exc
        return credentials.token

    def _client(self) -> httpx.Client:
        if getattr(self, "_cached_client", None) is None:
            token = self._access_token()
            self._cached_client = self.http(API_BASE, headers={"Authorization": f"Bearer {token}"})
        return self._cached_client

    def raise_for_status(self, response: httpx.Response, what: str) -> None:
        if response.status_code == 403 and _error_reasons(response) & RATE_LIMIT_REASONS:
            raise TransientError(
                f"Google Workspace limited the request rate while trying to {what}; "
                "it runs again later."
            )
        super().raise_for_status(response, what)

    def _request(self, method: str, path: str, what: str, *, allow: tuple[int, ...] = (), **kwargs):
        """A Directory API call. Status codes in `allow` are returned, not raised."""
        client = self._client()
        if allow:
            try:
                response = client.request(method, DIRECTORY + path, **kwargs)
            except httpx.TimeoutException as exc:
                raise TransientError(f"{self.name} did not answer in time ({what}).") from exc
            except httpx.TransportError as exc:
                raise TransientError(f"Could not reach {self.name} ({what}).") from exc
            if response.status_code in allow:
                return response
            self.raise_for_status(response, what)
            return response
        return self.call(client, method, DIRECTORY + path, what, **kwargs)

    def _pages(self, path: str, key: str, what: str, params: dict) -> Iterator[dict]:
        params = dict(params)
        while True:
            data = self._request("GET", path, what, params=params).json()
            yield from data.get(key) or []
            token = data.get("nextPageToken")
            if not token:
                return
            params["pageToken"] = token

    def hands_out_sign_in(self) -> bool:
        return bool(self.setting("deliver_initial_password", True))

    def test_connection(self) -> TestResult:
        self._request(
            "GET", "/users", "list users", params={"customer": self.customer, "maxResults": 1}
        )
        return TestResult(True, f"Connected to Google Workspace for {self.domain}.")

    # -- groups --------------------------------------------------------------------

    def list_groups(self) -> list[ExternalGroup]:
        return [
            ExternalGroup(
                external_id=group["id"],
                name=group.get("name") or group.get("email", ""),
                path=group.get("email", ""),
                description=group.get("description", ""),
            )
            for group in self._pages(
                "/groups", "groups", "list groups", {"customer": self.customer, "maxResults": 200}
            )
        ]

    def account_groups(self, account: ExternalAccount) -> set[str]:
        return {
            group["id"]
            for group in self._pages(
                "/groups",
                "groups",
                "list the user's groups",
                {"userKey": account.external_id, "maxResults": 200},
            )
        }

    def add_to_group(self, account: ExternalAccount, group_id: str) -> None:
        self._request(
            "POST",
            f"/groups/{quote(group_id, safe='')}/members",
            "add a group member",
            allow=(409,),
            json={"email": account.email, "role": "MEMBER"},
        )

    def remove_from_group(self, account: ExternalAccount, group_id: str) -> None:
        self._request(
            "DELETE",
            f"/groups/{quote(group_id, safe='')}/members/{quote(account.external_id, safe='')}",
            "remove a group member",
            allow=(404,),
        )

    # -- accounts ------------------------------------------------------------------

    @staticmethod
    def _account(user: dict) -> ExternalAccount:
        email = user.get("primaryEmail", "")
        name = user.get("name") or {}
        return ExternalAccount(
            external_id=str(user.get("id", "")),
            username=email,
            email=email,
            display_name=name.get("fullName", ""),
            active=user.get("suspended") is not True,
        )

    def _get_user(self, key: str) -> dict | None:
        response = self._request(
            "GET", f"/users/{quote(key, safe='@')}", "read a user", allow=(404,)
        )
        return None if response.status_code == 404 else response.json()

    def _work_email(self, spec: AccountSpec) -> str:
        email = (spec.email or "").strip().lower()
        if not email:
            raise AdapterError("Google Workspace accounts need a work email.")
        if email.rsplit("@", 1)[-1] != self.domain:
            raise AdapterError(f"The work email must be in the domain {self.domain}.")
        return email

    def _get_user_by_email(self, email: str) -> dict | None:
        """The user whose primary address this is. Google also answers for an
        alias, with the user that owns it: someone else's account."""
        user = self._get_user(email)
        if user is None or str(user.get("primaryEmail", "")).strip().lower() != email:
            return None
        return user

    def _alias_owner(self, email: str) -> bool:
        user = self._get_user(email)
        return user is not None and str(user.get("primaryEmail", "")).strip().lower() != email

    def find_account(self, spec: AccountSpec, external_id: str = "") -> ExternalAccount | None:
        user = self._get_user(external_id) if external_id else None
        if user is None and spec.email:
            user = self._get_user_by_email(spec.email.strip().lower())
        if user is None:
            return None
        self._last_user = user
        return self._account(user)

    def create_account(self, spec: AccountSpec) -> EnsureResult:
        email = self._work_email(spec)
        body = {
            "primaryEmail": email,
            "name": {"givenName": spec.first_name, "familyName": spec.last_name},
            "changePasswordAtNextLogin": True,
            "orgUnitPath": self.setting("org_unit_path", "/"),
        }
        if spec.personal_email:
            body["recoveryEmail"] = spec.personal_email
        deliver = bool(self.setting("deliver_initial_password", True))
        # Google requires a password; an undelivered one is random and unknown.
        password = generate_password() if deliver else generate_password(40)
        body["password"] = password
        try:
            response = self._request("POST", "/users", "create a user", allow=(409,), json=body)
        except TransientError:
            # The answer was lost, but Google may have created the user with
            # this password: a retry would find it and never send one.
            created = self.find_account(spec)
            if created is None:
                raise
            return EnsureResult(
                account=created,
                created=True,
                initial_password=password if deliver else None,
                notes=["Created the Google Workspace user; Google's answer was lost."],
            )
        if response.status_code == 409:
            existing = self.find_account(spec)
            if existing is None and self._alias_owner(email):
                raise AdapterError(_alias_refusal(email))
            if existing is None:
                raise TransientError("Google reported the user as existing but did not return it.")
            result = EnsureResult(account=existing)
            result.notes.append("The account already existed; no new password was set.")
            if not existing.active:
                result.account = self.set_active(existing, True)
                result.reactivated = True
            return result
        account = self._account(response.json())
        account.groups = frozenset()
        return EnsureResult(
            account=account,
            created=True,
            initial_password=password if deliver else None,
            notes=["Created the Google Workspace user."],
        )

    def plan_ensure(self, spec: AccountSpec, account: ExternalAccount | None) -> AccountPlan:
        plan = super().plan_ensure(spec, account)
        if plan.change == "create":
            try:
                email = self._work_email(spec)
            except AdapterError as exc:
                plan.refusal = str(exc)
            else:
                if self._alias_owner(email):
                    plan.refusal = _alias_refusal(email)
        elif plan.change == "reactivate":
            plan.refusal = _refusal(self._user_of(account), active=True)
        return plan

    def plan_disable(self, spec: AccountSpec, account: ExternalAccount | None) -> AccountPlan:
        plan = super().plan_disable(spec, account)
        if plan.change == "disable":
            plan.refusal = _refusal(self._user_of(account), active=False)
        return plan

    def _user_of(self, account: ExternalAccount) -> dict:
        user = getattr(self, "_last_user", None)
        if user is None or str(user.get("id")) != account.external_id:
            user = self._get_user(account.external_id) or {}
        return user

    def set_active(self, account: ExternalAccount, active: bool) -> ExternalAccount:
        refusal = _refusal(self._user_of(account), active)
        if refusal:
            raise AdapterError(refusal)
        response = self._request(
            "PUT",
            f"/users/{quote(account.external_id, safe='')}",
            "suspend or reactivate a user",
            json={"suspended": not active},
        )
        updated = self._account(response.json())
        updated.groups = account.groups
        return updated

    def list_accounts(self) -> Iterable[ExternalAccount]:
        for user in self._pages(
            "/users", "users", "list users", {"customer": self.customer, "maxResults": 500}
        ):
            yield self._account(user)

    def manual_instructions(self, action: str, spec: AccountSpec, groups: list[str]) -> str:
        who = f"{spec.display_name} ({spec.email or spec.username})"
        if action == "disable_account":
            return (
                f"In the Google Admin console, suspend the user {who} and remove them from "
                "the Google Groups this app manages."
            )
        lines = [f"In the Google Admin console, make sure {who} is an active user."]
        if groups:
            lines.append("Google Groups: " + ", ".join(groups) + ".")
        return "\n".join(lines)

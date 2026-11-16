"""The adapter interface. An adapter connects one kind of app or service.

Admins create connections (configured adapters) in Admin > Connections. The
form there is generated from `fields`; values of `secret` fields are stored
encrypted and never shown again.

Three kinds exist:

- provisioning: accounts and groups in an app (Keycloak, Google Workspace, ...).
  Implement the primitives below; `ensure_account` and `disable_account` combine
  them and rarely need overriding.
- hr_source: imports people (Personio). Implement `list_people`.
- email: sends notifications and sign-in details (SMTP). Implement `send`.

Adapters never touch the database: they get the configuration as a dict and
return plain data. Errors they raise carry messages safe to show to admins:
never put tokens, passwords or full provider responses in them.
"""

from __future__ import annotations

import secrets
import string
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date
from typing import Any, ClassVar

import httpx
from django.conf import settings


class AdapterKind:
    PROVISIONING = "provisioning"
    HR_SOURCE = "hr_source"
    EMAIL = "email"

    LABELS = {
        PROVISIONING: "Accounts and groups",
        HR_SOURCE: "People import",
        EMAIL: "Email",
    }


@dataclass(frozen=True)
class Field:
    """One setting of a connection, shown in the admin form."""

    name: str
    label: str
    # text, url, email, int, bool, choice, textarea, secret, secret_text
    kind: str = "text"
    required: bool = False
    help: str = ""
    default: Any = None
    choices: tuple[tuple[str, str], ...] = ()
    placeholder: str = ""

    @property
    def secret(self) -> bool:
        return self.kind in {"secret", "secret_text"}


SYNC_INTERVAL_FIELD = Field(
    "sync_interval_minutes",
    "Compare accounts every (minutes)",
    kind="int",
    default=360,
    help="How often the worker reads all accounts to find drift. 0 turns it off.",
)


class AdapterError(Exception):
    """A provider call failed for a reason that retrying will not fix soon:
    bad credentials, missing permissions, invalid data. The message is shown
    to admins and must not contain secrets."""

    retryable = False


class TransientError(AdapterError):
    """Timeouts, rate limits and server errors: the job runs again later."""

    retryable = True


class ConfigurationError(AdapterError):
    """The connection's settings or credentials are wrong or incomplete."""


@dataclass
class ExternalAccount:
    external_id: str
    username: str = ""
    email: str = ""
    display_name: str = ""
    active: bool = True
    # External IDs of the groups the account is in; None when not read.
    groups: frozenset[str] | None = None


@dataclass
class ExternalGroup:
    external_id: str
    name: str
    path: str = ""
    description: str = ""


@dataclass
class AccountSpec:
    """What the app knows about the person an account is for."""

    first_name: str
    last_name: str
    username: str
    email: str = ""
    personal_email: str = ""
    job_title: str = ""
    person_type: str = ""
    team: str = ""
    manager_email: str = ""
    start_date: date | None = None
    end_date: date | None = None

    @property
    def display_name(self) -> str:
        return f"{self.first_name} {self.last_name}".strip()


@dataclass
class EnsureResult:
    account: ExternalAccount
    created: bool = False
    reactivated: bool = False
    groups_added: list[str] = field(default_factory=list)
    groups_removed: list[str] = field(default_factory=list)
    # Generated for a new account and delivered by email; never stored.
    initial_password: str | None = None
    notes: list[str] = field(default_factory=list)
    # Why the groups were left unfinished. The account step still counts:
    # a group never holds up onboarding.
    group_error: Exception | None = None


@dataclass
class DisableResult:
    account: ExternalAccount | None
    changed: bool = False
    groups_removed: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    # Why the groups were left unfinished. The account is disabled anyway: a
    # group never holds up offboarding.
    group_error: Exception | None = None


@dataclass
class TestResult:
    ok: bool
    message: str


@dataclass
class HRRecord:
    """One person as an HR system lists them."""

    external_id: str
    first_name: str
    last_name: str
    work_email: str = ""
    personal_email: str = ""
    job_title: str = ""
    person_type: str = ""
    team: str = ""
    manager_email: str = ""
    start_date: date | None = None
    end_date: date | None = None
    # False once the HR system marks the person as left or inactive.
    active: bool = True


def generate_password(length: int = 20) -> str:
    alphabet = string.ascii_letters + string.digits + "-_.!?"
    while True:
        value = "".join(secrets.choice(alphabet) for _ in range(length))
        if (
            any(c.islower() for c in value)
            and any(c.isupper() for c in value)
            and any(c.isdigit() for c in value)
            and any(c in "-_.!?" for c in value)
        ):
            return value


class Adapter:
    # Templates show adapter classes; never let them instantiate one.
    do_not_call_in_templates: ClassVar[bool] = True
    slug: ClassVar[str] = ""
    name: ClassVar[str] = ""
    kind: ClassVar[str] = AdapterKind.PROVISIONING
    description: ClassVar[str] = ""
    fields: ClassVar[tuple[Field, ...]] = ()
    # Provisioning capabilities.
    supports_write: ClassVar[bool] = True
    supports_groups: ClassVar[bool] = False
    supports_account_listing: ClassVar[bool] = True
    # Whether admins may add groups by hand (apps without a group API).
    manual_groups: ClassVar[bool] = False
    # Remove managed groups when an account is disabled.
    remove_groups_on_disable: ClassVar[bool] = True
    # What a group is called in this app (shown in forms).
    group_label: ClassVar[str] = "Groups"

    @classmethod
    def groups_enabled(cls, settings: dict) -> bool:
        """Whether a connection with these (non-secret) settings has groups."""
        return cls.supports_groups

    @classmethod
    def imports_groups(cls) -> bool:
        return cls.supports_groups and cls.list_groups is not Adapter.list_groups

    def __init__(
        self, config: dict, *, connection=None, transport: httpx.BaseTransport | None = None
    ):
        self.config = {f.name: f.default for f in self.fields if f.default is not None}
        self.config.update({k: v for k, v in config.items() if v not in (None, "")})
        self.connection = connection
        self.transport = transport

    # -- helpers -----------------------------------------------------------

    def setting(self, name: str, default: Any = None) -> Any:
        value = self.config.get(name)
        return default if value in (None, "") else value

    def require(self, *names: str) -> None:
        labels = {f.name: f.label for f in self.fields}
        missing = [labels.get(n, n) for n in names if self.setting(n) in (None, "")]
        if missing:
            raise ConfigurationError(f"Missing settings: {', '.join(missing)}.")

    def http(self, base_url: str = "", **kwargs) -> httpx.Client:
        return httpx.Client(
            base_url=base_url,
            timeout=kwargs.pop("timeout", settings.PROVIDER_TIMEOUT_SECONDS),
            transport=self.transport,
            follow_redirects=False,
            headers={"User-Agent": "meldgrove-iam", **kwargs.pop("headers", {})},
            **kwargs,
        )

    def raise_for_status(self, response: httpx.Response, what: str) -> None:
        """Turn an HTTP error status into an adapter error without echoing
        the response body, which can contain request data."""
        status = response.status_code
        if status < 400:
            return
        if status == 429 or status >= 500:
            raise TransientError(f"{self.name} answered {status} while trying to {what}.")
        if status in (401, 403):
            raise ConfigurationError(
                f"{self.name} refused access ({status}) while trying to {what}. "
                "Check the connection's credentials and permissions."
            )
        raise AdapterError(f"{self.name} answered {status} while trying to {what}.")

    def call(
        self, client: httpx.Client, method: str, url: str, what: str, **kwargs
    ) -> httpx.Response:
        try:
            response = client.request(method, url, **kwargs)
        except httpx.TimeoutException as exc:
            raise TransientError(f"{self.name} did not answer in time ({what}).") from exc
        except httpx.TransportError as exc:
            raise TransientError(f"Could not reach {self.name} ({what}).") from exc
        self.raise_for_status(response, what)
        return response

    # -- all kinds -----------------------------------------------------------

    def test_connection(self) -> TestResult:
        raise NotImplementedError

    # -- provisioning primitives --------------------------------------------

    def list_groups(self) -> list[ExternalGroup]:
        return []

    def find_account(self, spec: AccountSpec, external_id: str = "") -> ExternalAccount | None:
        raise NotImplementedError

    def create_account(self, spec: AccountSpec) -> EnsureResult:
        raise NotImplementedError

    def set_active(self, account: ExternalAccount, active: bool) -> ExternalAccount:
        raise NotImplementedError

    def account_groups(self, account: ExternalAccount) -> set[str]:
        return set(account.groups or ())

    def add_to_group(self, account: ExternalAccount, group_id: str) -> None:
        raise NotImplementedError

    def remove_from_group(self, account: ExternalAccount, group_id: str) -> None:
        raise NotImplementedError

    def list_accounts(self) -> Iterable[ExternalAccount]:
        return []

    def manual_instructions(self, action: str, spec: AccountSpec, groups: list[str]) -> str:
        """Steps for a person when this app cannot or may not make a change."""
        who = f"{spec.display_name} ({spec.email or spec.username})"
        if action == "disable_account":
            return f"Remove or disable the {self.name} account of {who}."
        lines = [f"Make sure {who} has an active {self.name} account."]
        if groups:
            lines.append(f"{self.group_label}: " + ", ".join(groups) + ".")
        return "\n".join(lines)

    # -- provisioning operations ----------------------------------------------

    def ensure_account(
        self,
        spec: AccountSpec,
        *,
        external_id: str = "",
        groups: set[str],
        managed_groups: set[str],
    ) -> EnsureResult:
        """Make the account exist, be active and be in exactly the wanted
        managed groups. Groups the app does not manage stay as they are. A
        failing group step ends in `group_error`, never in an exception."""
        account = self.find_account(spec, external_id)
        if account is None:
            result = self.create_account(spec)
            account = result.account
        else:
            result = EnsureResult(account=account)
            if not account.active:
                account = self.set_active(account, True)
                result.account = account
                result.reactivated = True
        if self.supports_groups:
            try:
                current = self.account_groups(account)
                for group_id in sorted(groups - current):
                    self.add_to_group(account, group_id)
                    result.groups_added.append(group_id)
                for group_id in sorted((current & managed_groups) - groups):
                    self.remove_from_group(account, group_id)
                    result.groups_removed.append(group_id)
            except Exception as exc:  # noqa: BLE001 - the engine reports it
                result.group_error = exc
                account.groups = None
            else:
                account.groups = frozenset((current | groups) - set(result.groups_removed))
        return result

    def disable_account(
        self, spec: AccountSpec, *, external_id: str = "", managed_groups: set[str]
    ) -> DisableResult:
        account = self.find_account(spec, external_id)
        if account is None:
            return DisableResult(account=None, notes=["No account found; nothing to disable."])
        result = DisableResult(account=account)
        if self.supports_groups and self.remove_groups_on_disable:
            try:
                current = self.account_groups(account)
                for group_id in sorted(current & managed_groups):
                    self.remove_from_group(account, group_id)
                    result.groups_removed.append(group_id)
            except Exception as exc:  # noqa: BLE001 - the engine reports it
                result.group_error = exc
                account.groups = None
            else:
                account.groups = frozenset(current - set(result.groups_removed))
        if account.active:
            result.account = self.set_active(account, False)
            result.changed = True
        return result

    # -- other kinds -----------------------------------------------------------

    def list_people(self) -> Iterable[HRRecord]:
        raise NotImplementedError

    def send(self, *, to: list[str], subject: str, body: str, reply_to: str = "") -> None:
        raise NotImplementedError

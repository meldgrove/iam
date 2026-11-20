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

import email.utils
import secrets
import string
import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any, ClassVar

import httpx
from django.conf import settings

from ..liveness import note_progress


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
    # Decides where the secrets are sent (a URL, a host, encryption on or
    # off). Changing it needs the saved secrets entered again.
    destination: bool = False

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


class ReadOnlyError(AdapterError):
    """Read-only mode stopped a change to a connected app, or an email."""


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
    # Why the adapter could not arrange the first sign-in of a new account;
    # the engine asks a person to do it.
    credentials_problem: str = ""
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


# Default of `found`: the operation looks the account up itself.
LOOK_UP = object()

# Answers to a read that a moment later can turn out fine.
RETRY_STATUSES = frozenset({429, 502, 503, 504})
# Pauses before the second and the third try of a read.
RETRY_PAUSES = (1.0, 4.0)
# The pauses of one request add up to at most this, which also bounds how
# long a page that reads a connected app waits. A server that asks for more
# gets it from the job's or the read's own retry, later.
MAX_RETRY_WAIT_SECONDS = 30.0


def pause(seconds: float) -> None:
    time.sleep(seconds)


def retry_after(response: httpx.Response) -> float | None:
    """The pause a Retry-After header asks for, in seconds."""
    value = response.headers.get("Retry-After", "").strip()
    if not value:
        return None
    if value.isascii() and value.isdigit():
        return float(value)
    try:
        when = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return max((when - datetime.now(UTC)).total_seconds(), 0.0)


class ProviderClient(httpx.Client):
    """The HTTP client of every adapter.

    A read (GET or HEAD) that a rate limit, a 502, 503 or 504, or a refused
    connection stops is tried up to twice more, after the pause the server
    asks for or a short one, so one hiccup in a long listing does not fail
    the whole read. A connection that timed out is not tried again: the
    next try would wait as long. Changes are never sent twice here: whether a change
    landed is unknown, and the adapters handle a lost answer themselves.
    Every answer counts as progress for the worker's watchdog."""

    def send(self, request: httpx.Request, **kwargs) -> httpx.Response:
        pauses = list(RETRY_PAUSES) if request.method in ("GET", "HEAD") else []
        budget = MAX_RETRY_WAIT_SECONDS
        while True:
            try:
                response = super().send(request, **kwargs)
            except httpx.ConnectError:
                if not pauses:
                    raise
                wait = pauses.pop(0)
            else:
                note_progress()
                if not pauses or response.status_code not in RETRY_STATUSES:
                    return response
                asked = retry_after(response)
                wait = pauses.pop(0)
                if asked is not None:
                    wait = asked
                if wait > budget:
                    return response
                response.close()
            budget -= wait
            pause(wait)


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
        return ProviderClient(
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

    def check_same_email(self, username: str, emails: Iterable, spec: AccountSpec) -> None:
        """Stop at a user found by username whose emails are known and none
        is the person's work email. A username can belong to someone else
        (another J. Smith); a different email shows it does."""
        known = {str(email or "").strip().lower() for email in emails} - {""}
        if known and spec.email and spec.email.strip().lower() not in known:
            raise AdapterError(
                f"The {self.name} username {username} belongs to a user with a different "
                "email. Nothing was changed. Check the person's username and work email."
            )

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

    def hands_out_sign_in(self) -> bool:
        """Whether creating an account arranges its first sign-in, such as an
        initial password or a setup email. A worker that stops between the
        create and recording it loses that, and a person must check it."""
        return False

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
        found: ExternalAccount | None | object = LOOK_UP,
    ) -> EnsureResult:
        """Make the account exist, be active and be in exactly the wanted
        managed groups. Groups the app does not manage stay as they are. A
        failing group step ends in `group_error`, never in an exception.
        `found` is the result of a `find_account` the caller already checked;
        the operation then changes that account, not one it looks up again."""
        account = self.find_account(spec, external_id) if found is LOOK_UP else found
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
        self,
        spec: AccountSpec,
        *,
        external_id: str = "",
        managed_groups: set[str],
        found: ExternalAccount | None | object = LOOK_UP,
    ) -> DisableResult:
        account = self.find_account(spec, external_id) if found is LOOK_UP else found
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


class ReadOnlyAdapter:
    """An adapter that can only read. In read-only mode (READ_ONLY=1),
    Connection.build_adapter hands this out instead of the adapter itself.
    Everything not named in READS, such as creating an account, changing
    groups or sending email, raises ReadOnlyError before it reaches the
    connected app. An operation added later stays blocked until it is
    listed here as a read."""

    READS = frozenset(
        {
            # What the adapter is.
            "name",
            "slug",
            "kind",
            "group_label",
            "supports_groups",
            "supports_account_listing",
            "remove_groups_on_disable",
            # Reads. Some sign in first (an access token); none changes anything.
            "test_connection",
            "list_groups",
            "list_accounts",
            "can_read_accounts",
            "find_account",
            "account_groups",
            "list_people",
            "skipped",
            "manual_instructions",
        }
    )

    def __init__(self, adapter: Adapter):
        self._adapter = adapter

    def __getattr__(self, name: str):
        if name.startswith("_"):
            raise AttributeError(name)
        if name in self.READS:
            return getattr(self._adapter, name)
        raise ReadOnlyError(
            f"Read-only mode: {self._adapter.name} was not changed ({name.replace('_', ' ')})."
        )

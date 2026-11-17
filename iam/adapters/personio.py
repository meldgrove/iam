"""Personio: imports people from the HR system. Read only.

Uses the Personio Personnel Data API v1: POST /v1/auth with the API
credentials returns a bearer token; GET /v1/company/employees lists people
page by page. Custom attributes (private email, planned start date,
supervisor) are found by their label through
GET /v1/company/employees/attributes and requested by key.

The adapter only reads Personio and returns HRRecord objects; the app matches
and creates people. Records it cannot read (missing name, unreadable date) are
skipped and listed in `skipped`, so one bad record never blocks the rest.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from datetime import date, datetime, timedelta

from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.utils import timezone

from .base import (
    Adapter,
    AdapterError,
    AdapterKind,
    ConfigurationError,
    Field,
    HRRecord,
    TestResult,
)

PAGE_SIZE = 100
PERSON_TYPES = {"employee", "intern", "freelancer", "volunteer", "external"}
# Personio's employment_type is "internal" or "external".
DEFAULT_TYPE_MAPPING = {"internal": "employee", "external": "external"}
INACTIVE_STATUSES = {"inactive", "terminated", "terminated_with_access"}
EMPLOYMENT_TYPE_KEYS = ("employment_type", "employmentType", "Employment type")
POSITION_KEYS = ("position", "Position", "job_title")
PRIVATE_EMAIL_KEYS = (
    "private_email",
    "personal_email",
    "email_private",
    "privateEmail",
    "personalEmail",
    "private_email_address",
    "personal_email_address",
    "Private E-Mail-Adresse",
    "private e-mail adresse",
    "private email adresse",
    "private e-mail",
    "personal e-mail",
    "private email",
    "personal email",
    "e-mail privat",
    "email privat",
    "private mail",
    "privatmail",
)
START_DATE_KEYS = (
    "hire_date",
    "start_date",
    "planned_start_date",
    "plannedStartDate",
    "planned start date",
    "planned hire date",
    "planned employment start date",
    "planned_start",
    "geplanter_start",
    "geplanter start",
    "geplanter eintritt",
    "geplantes eintrittsdatum",
    "geplanter starttermin",
    "starttermin",
    "eintrittsdatum",
    "eintritt",
)
SUPERVISOR_KEYS = (
    "supervisor",
    "Supervisor",
    "responsible_person",
    "responsiblePerson",
    "Responsible person",
    "workplaces_supervisor",
    "workplacesSupervisor",
    "Workplaces Supervisor",
    "Workplace Supervisor",
)
STANDARD_ATTRIBUTE_KEYS = (
    "first_name",
    "last_name",
    "email",
    "position",
    "department",
    "team",
    "status",
    "employment_type",
    *START_DATE_KEYS,
    "end_date",
    "contract_end_date",
    "termination_date",
    "private_email",
    "personal_email",
    "email_private",
    "private_email_address",
    "personal_email_address",
    *SUPERVISOR_KEYS,
)


class RecordError(ValueError):
    """One employee record cannot be read."""


def normalized_key(key: str) -> str:
    return re.sub(r"[^a-z0-9]", "", key.lower())


def attribute_aliases(key: str, value) -> set[str]:
    """A key plus the label and universal ID Personio sends with it."""
    aliases = {normalized_key(key)}
    if isinstance(value, dict):
        for metadata_key in ("label", "universal_id"):
            metadata_value = value.get(metadata_key)
            if metadata_value:
                aliases.add(normalized_key(str(metadata_value)))
    return aliases


def plain_value(value):
    """Unwrap Personio's {"value": ...} and {"attributes": {"name": ...}} shapes."""
    if isinstance(value, dict) and "value" in value:
        return plain_value(value["value"])
    if isinstance(value, dict):
        attributes = value.get("attributes")
        if isinstance(attributes, dict):
            for key in ("name", "label", "title"):
                if attributes.get(key):
                    return plain_value(attributes[key])
        for key in ("name", "label", "title"):
            if value.get(key):
                return plain_value(value[key])
        return ""
    return value


def _by_alias(attributes: dict) -> dict:
    found = {}
    for key, value in attributes.items():
        for alias in attribute_aliases(str(key), value):
            found.setdefault(alias, value)
    return found


def _raw(attributes: dict, keys: Iterable[str]):
    aliases = _by_alias(attributes)
    for key in keys:
        value = attributes[key] if key in attributes else aliases.get(normalized_key(key))
        if value not in (None, ""):
            return value
    return None


def text(attributes: dict, *keys: str) -> str:
    aliases = _by_alias(attributes)
    for key in keys:
        value = plain_value(
            attributes[key] if key in attributes else aliases.get(normalized_key(key))
        )
        if value not in (None, ""):
            return str(value).strip()
    return ""


def parse_date(attributes: dict, *keys: str) -> date | None:
    value = text(attributes, *keys)
    if not value:
        return None
    try:
        return datetime.strptime(value, "%d.%m.%Y").date()
    except ValueError:
        pass
    try:
        return date.fromisoformat(value)
    except ValueError:
        try:
            return datetime.fromisoformat(value).date()
        except ValueError as exc:
            raise RecordError(f"unreadable date in {keys[0]}") from exc


def supervisor_reference(attributes: dict) -> tuple[str, str]:
    """The supervisor's Personio ID and email (or name when there is none)."""
    raw = _raw(attributes, SUPERVISOR_KEYS)
    if raw in (None, ""):
        return "", ""
    value = raw.get("value", raw) if isinstance(raw, dict) else raw
    if isinstance(value, list):
        value = value[0] if value else None
    if not isinstance(value, dict):
        return "", str(value).strip() if value not in (None, "") else ""
    employee = value.get("attributes")
    if not isinstance(employee, dict):
        return str(plain_value(value.get("id")) or "").strip(), str(plain_value(value) or "")
    source_id = str(plain_value(employee.get("id")) or "").strip()
    email = text(employee, "email")
    if email:
        return source_id, email
    name = text(employee, "display_name", "preferred_name")
    if not name:
        name = f"{text(employee, 'first_name')} {text(employee, 'last_name')}".strip()
    return source_id, name


def team_name(attributes: dict, attribute: str) -> str:
    keys = [attribute]
    if normalized_key(attribute) in {"department", "team"}:
        keys = ["department", "team"]
    raw = _raw(attributes, keys)
    value = raw.get("value", raw) if isinstance(raw, dict) else raw
    if isinstance(value, list):
        value = value[0] if value else None
    if isinstance(value, dict):
        inner = value.get("attributes", value)
        if isinstance(inner, dict):
            return text(inner, "name", "label", "title")
        return str(plain_value(value) or "").strip()
    return str(value or "").strip()


def record_id(raw) -> str:
    if not isinstance(raw, dict):
        return ""
    attributes = raw.get("attributes", raw)
    nested = plain_value(attributes.get("id")) if isinstance(attributes, dict) else ""
    return str(raw.get("id") or nested or "").strip()


def is_email(value: str) -> bool:
    if not value:
        return False
    try:
        validate_email(value)
    except ValidationError:
        return False
    return True


class PersonioAdapter(Adapter):
    slug = "personio"
    name = "Personio"
    kind = AdapterKind.HR_SOURCE
    description = (
        "Imports people from Personio: names, emails, team, manager, start and end dates. "
        "Read only; nothing is written to Personio."
    )
    supports_write = False
    supports_account_listing = False
    fields = (
        Field(
            "client_id",
            "Client ID",
            required=True,
            help="Personio > Settings > Integrations > API credentials.",
        ),
        Field("client_secret", "Client secret", kind="secret", required=True),
        Field(
            "base_url",
            "API URL",
            kind="url",
            default="https://api.personio.de",
            destination=True,
        ),
        Field(
            "work_email_domains",
            "Work email domains",
            help=(
                "Comma-separated, e.g. example.invalid. Personio's email outside these domains "
                "becomes the personal email. Empty: Personio's email is the work email."
            ),
        ),
        Field(
            "type_mapping",
            "Person types",
            kind="textarea",
            help=(
                "One line per value: 'Personio value = employee|intern|freelancer|volunteer|"
                "external'. Matched against the employment type (internal, external), then "
                "the position. Without a match: internal = employee, external = external, "
                "anything else = employee."
            ),
            placeholder="Werkstudent = intern\nFreelancer = freelancer",
        ),
        Field(
            "team_attribute",
            "Team attribute",
            default="department",
            help="The Personio attribute whose value names the team, e.g. department.",
        ),
        Field(
            "include_inactive_days",
            "List leavers for (days)",
            kind="int",
            default=30,
            help="People who left longer ago are no longer imported.",
        ),
        Field(
            "sync_interval_minutes",
            "Import people every (minutes)",
            kind="int",
            default=60,
            help="0 imports only when started by hand.",
        ),
    )

    def __init__(self, config: dict, **kwargs):
        super().__init__(config, **kwargs)
        self._token = ""
        self._attribute_keys: list[str] | None = None
        # (Personio ID, reason) of records that could not be read in the last run.
        self.skipped: list[tuple[str, str]] = []

    # -- API -----------------------------------------------------------------

    def _client(self):
        self.require("client_id", "client_secret")
        base_url = str(self.setting("base_url")).rstrip("/")
        if not base_url.startswith(("https://", "http://")):
            raise ConfigurationError("The API URL must start with https://.")
        return self.http(base_url, headers={"Accept": "application/json"})

    def _json(self, response, what: str) -> dict:
        try:
            data = response.json()
        except ValueError as exc:
            raise AdapterError(f"Personio sent an unreadable answer ({what}).") from exc
        if not isinstance(data, dict):
            raise AdapterError(f"Personio sent an unexpected answer ({what}).")
        return data

    def _request(self, client, method: str, path: str, what: str, **kwargs) -> dict:
        headers = kwargs.pop("headers", {})
        headers["Authorization"] = f"Bearer {self._access_token(client)}"
        response = self.call(client, method, path, what, headers=headers, **kwargs)
        return self._json(response, what)

    def _access_token(self, client) -> str:
        if self._token:
            return self._token
        response = self.call(
            client,
            "POST",
            "/v1/auth",
            "sign in",
            json={
                "client_id": self.setting("client_id"),
                "client_secret": self.setting("client_secret"),
            },
        )
        data = self._json(response, "sign in")
        payload = data.get("data")
        token = payload.get("token") if isinstance(payload, dict) else data.get("token")
        if not token:
            raise ConfigurationError("Personio accepted the sign-in but sent no token.")
        self._token = str(token)
        return self._token

    def _employee_attribute_keys(self, client) -> list[str]:
        """Standard keys plus custom attributes whose label names a private
        email, a start date or a supervisor, and the team attribute."""
        if self._attribute_keys is not None:
            return self._attribute_keys
        keys = list(STANDARD_ATTRIBUTE_KEYS)
        team_attribute = str(self.setting("team_attribute", "department"))
        if team_attribute not in keys:
            keys.append(team_attribute)
        wanted = {
            normalized_key(k) for k in (*PRIVATE_EMAIL_KEYS, *START_DATE_KEYS, *SUPERVISOR_KEYS)
        }
        wanted.add(normalized_key(team_attribute))
        data = self._request(client, "GET", "/v1/company/employees/attributes", "read attributes")
        attributes = data.get("data")
        if not isinstance(attributes, list):
            raise AdapterError("Personio sent an unexpected attribute list.")
        for attribute in attributes:
            if not isinstance(attribute, dict) or not attribute.get("key"):
                continue
            key = str(attribute["key"])
            if attribute_aliases(key, attribute) & wanted and key not in keys:
                keys.append(key)
        self._attribute_keys = keys
        return keys

    def _employee_pages(self, client, *, max_pages: int | None = None):
        keys = self._employee_attribute_keys(client)
        offset = 0
        pages = 0
        while True:
            data = self._request(
                client,
                "GET",
                "/v1/company/employees",
                "list employees",
                params={"offset": offset, "limit": PAGE_SIZE, "attributes[]": keys},
            )
            page = data.get("data")
            if not isinstance(page, list):
                raise AdapterError("Personio sent an unexpected employee list.")
            yield data, page
            pages += 1
            if max_pages is not None and pages >= max_pages:
                return
            metadata = data.get("metadata")
            if isinstance(metadata, dict):
                current, total = metadata.get("current_page"), metadata.get("total_pages")
                if current is not None and total is not None:
                    if int(current) >= int(total) - 1:
                        return
                    offset += PAGE_SIZE
                    continue
            if len(page) < PAGE_SIZE:
                return
            offset += PAGE_SIZE

    # -- mapping ---------------------------------------------------------------

    def _configured_types(self) -> dict[str, str]:
        mapping = {}
        for line in str(self.setting("type_mapping", "")).splitlines():
            if "=" not in line:
                continue
            source, target = (part.strip() for part in line.split("=", 1))
            target = target.lower()
            if source and target in PERSON_TYPES:
                mapping[source.casefold()] = target
        return mapping

    def _person_type(self, employment_type: str, position: str) -> str:
        """Configured employment type, then configured position, then the
        default for the employment type, then employee."""
        configured = self._configured_types()
        for value in (employment_type, position):
            if value and value.casefold() in configured:
                return configured[value.casefold()]
        return DEFAULT_TYPE_MAPPING.get(employment_type.casefold(), "employee")

    def _work_domains(self) -> set[str]:
        return {
            d.strip().lower().lstrip("@")
            for d in str(self.setting("work_email_domains", "")).split(",")
            if d.strip()
        }

    def _record(self, raw: dict, today: date) -> tuple[HRRecord, str, str] | None:
        """The record, the supervisor's Personio ID, and the supervisor reference.
        None when the person left before the listing window."""
        if not isinstance(raw, dict):
            raise RecordError("not an object")
        attributes = raw.get("attributes", raw)
        if not isinstance(attributes, dict):
            raise RecordError("attributes are not an object")
        external_id = record_id(raw)
        first_name = text(attributes, "first_name", "firstName")
        last_name = text(attributes, "last_name", "lastName")
        if not external_id or not first_name or not last_name:
            raise RecordError("missing ID or name")

        email = text(attributes, "email").lower()
        personal_email = text(attributes, *PRIVATE_EMAIL_KEYS).lower()
        domains = self._work_domains()
        work_email = email if is_email(email) else ""
        if work_email and domains and work_email.rsplit("@", 1)[1] not in domains:
            if not personal_email:
                personal_email = work_email
            work_email = ""
        if not is_email(personal_email):
            personal_email = ""

        status = text(attributes, "status").lower()
        contract_end = parse_date(attributes, "end_date", "contract_end_date")
        termination = parse_date(attributes, "termination_date")
        # The termination date wins over the contract end date; the
        # "last day at work" is informational and never ends access.
        end_date = termination or contract_end
        active = status not in INACTIVE_STATUSES and not (end_date and end_date < today)
        # Leavers stay listed for a while so the app sees them leave; without
        # an end date the app cannot tell when, so they stay listed.
        window = timedelta(days=int(self.setting("include_inactive_days", 30) or 0))
        if not active and end_date is not None and end_date < today - window:
            return None

        employment_type = text(attributes, *EMPLOYMENT_TYPE_KEYS)
        position = text(attributes, *POSITION_KEYS)
        person_type = self._person_type(employment_type, position)

        supervisor_id, supervisor = supervisor_reference(attributes)
        record = HRRecord(
            external_id=external_id,
            first_name=first_name,
            last_name=last_name,
            work_email=work_email,
            personal_email=personal_email,
            job_title=position,
            person_type=person_type,
            team=team_name(attributes, str(self.setting("team_attribute", "department"))),
            manager_email=supervisor.lower() if is_email(supervisor) else "",
            start_date=parse_date(attributes, *START_DATE_KEYS),
            end_date=end_date,
            active=active,
        )
        return record, supervisor_id, supervisor

    # -- adapter interface -----------------------------------------------------

    def test_connection(self) -> TestResult:
        with self._client() as client:
            data, page = next(self._employee_pages(client, max_pages=1))
        metadata = data.get("metadata") if isinstance(data.get("metadata"), dict) else {}
        total = metadata.get("total_elements")
        if total is None and int(metadata.get("total_pages") or 1) <= 1:
            total = len(page)
        if total is None:
            return TestResult(True, "Connected to Personio and read the first employees.")
        noun = "employee" if int(total) == 1 else "employees"
        return TestResult(True, f"Connected to Personio: {total} {noun}.")

    def list_people(self) -> Iterable[HRRecord]:
        self.skipped = []
        today = timezone.localdate()
        entries: list[tuple[HRRecord, str, str]] = []
        with self._client() as client:
            for _, page in self._employee_pages(client):
                for raw in page:
                    try:
                        entry = self._record(raw, today)
                    except RecordError as exc:
                        self.skipped.append((record_id(raw), str(exc)))
                        continue
                    if entry is not None:
                        entries.append(entry)
        # A supervisor given only by Personio ID gets that person's work email.
        emails = {record.external_id: record.work_email for record, _, _ in entries}
        for record, supervisor_id, _ in entries:
            if not record.manager_email and supervisor_id:
                record.manager_email = emails.get(supervisor_id, "")
        return [record for record, _, _ in entries]

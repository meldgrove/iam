import json

import httpx
import pytest

from iam.adapters.base import (
    AccountSpec,
    AdapterError,
    ConfigurationError,
    TransientError,
)
from iam.adapters.google_workspace import GoogleWorkspaceAdapter

PREFIX = "/admin/directory/v1"


def make_adapter(transport, **config):
    adapter = GoogleWorkspaceAdapter(
        {
            "service_account_json": "{}",
            "admin_email": "admin@example.invalid",
            "domain": "example.invalid",
            **config,
        },
        transport=transport,
    )
    adapter._access_token = lambda: "test-token"
    return adapter


def spec(**overrides):
    values = {
        "first_name": "Ada",
        "last_name": "Lovelace",
        "username": "ada",
        "email": "ada@example.invalid",
        "personal_email": "ada.private@example.invalid",
    }
    values.update(overrides)
    return AccountSpec(**values)


def user(user_id="u1", email="ada@example.invalid", suspended=False, admin=False):
    return {
        "id": user_id,
        "primaryEmail": email,
        "name": {"fullName": "Ada Lovelace"},
        "suspended": suspended,
        "isAdmin": admin,
        "isDelegatedAdmin": False,
    }


class Directory:
    """A small in-memory Directory API."""

    def __init__(self, users=None, memberships=None):
        self.users = {u["id"]: u for u in users or []}
        self.memberships = memberships or {}  # group id -> set of user ids
        self.created = []
        self.member_status = None

    def by_key(self, key):
        for u in self.users.values():
            if key in (u["id"], u["primaryEmail"]):
                return u
        return None

    def __call__(self, request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer test-token"
        path = request.url.path.removeprefix(PREFIX)
        params = request.url.params
        if request.method == "GET" and path == "/groups" and "userKey" in params:
            uid = params["userKey"]
            groups = [{"id": g} for g, members in self.memberships.items() if uid in members]
            return httpx.Response(200, json={"groups": groups})
        if request.method == "GET" and path.startswith("/users/"):
            found = self.by_key(path.split("/", 2)[2])
            return httpx.Response(200, json=found) if found else httpx.Response(404, json={})
        if request.method == "POST" and path == "/users":
            body = json.loads(request.content)
            if self.by_key(body["primaryEmail"]):
                return httpx.Response(409, json={})
            self.created.append(body)
            new = user("new1", body["primaryEmail"])
            self.users[new["id"]] = new
            return httpx.Response(200, json=new)
        if request.method == "PUT" and path.startswith("/users/"):
            found = self.by_key(path.split("/", 2)[2])
            found.update(json.loads(request.content))
            return httpx.Response(200, json=found)
        if request.method == "POST" and path.endswith("/members"):
            if self.member_status:
                return httpx.Response(self.member_status, json={})
            group = path.split("/")[2]
            email = json.loads(request.content)["email"]
            self.memberships.setdefault(group, set()).add(self.by_key(email)["id"])
            return httpx.Response(200, json={})
        if request.method == "DELETE" and "/members/" in path:
            _, _, group, _, member = path.split("/")
            if member not in self.memberships.get(group, set()):
                return httpx.Response(404, json={})
            self.memberships[group].discard(member)
            return httpx.Response(204)
        raise AssertionError(f"unexpected {request.method} {request.url}")


def test_connection_ok(mock_transport):
    transport = mock_transport(lambda r: httpx.Response(200, json={"users": [user()]}))
    result = make_adapter(transport).test_connection()
    assert result.ok
    assert "example.invalid" in result.message
    request = transport.requests[0]
    assert request.url.params["customer"] == "my_customer"
    assert request.url.params["maxResults"] == "1"


def test_invalid_key_is_a_configuration_error():
    adapter = GoogleWorkspaceAdapter(
        {
            "service_account_json": "not json",
            "admin_email": "admin@example.invalid",
            "domain": "example.invalid",
        }
    )
    with pytest.raises(ConfigurationError) as excinfo:
        adapter.test_connection()
    assert "not json" not in str(excinfo.value)


def test_missing_settings_are_a_configuration_error():
    with pytest.raises(ConfigurationError):
        GoogleWorkspaceAdapter({})._access_token()


def test_list_groups_follows_pages(mock_transport):
    pages = {
        None: {
            "groups": [{"id": "g1", "name": "Staff", "email": "staff@example.invalid"}],
            "nextPageToken": "p2",
        },
        "p2": {"groups": [{"id": "g2", "email": "all@example.invalid", "description": "All"}]},
    }
    transport = mock_transport(
        lambda r: httpx.Response(200, json=pages[r.url.params.get("pageToken")])
    )
    groups = make_adapter(transport).list_groups()
    assert [(g.external_id, g.name, g.path) for g in groups] == [
        ("g1", "Staff", "staff@example.invalid"),
        ("g2", "all@example.invalid", "all@example.invalid"),
    ]
    assert groups[1].description == "All"


def test_ensure_creates_user_with_initial_password_and_groups(mock_transport):
    directory = Directory()
    adapter = make_adapter(mock_transport(directory), org_unit_path="/Staff")
    result = adapter.ensure_account(spec(), groups={"g1", "g2"}, managed_groups={"g1", "g2"})
    assert result.created
    assert result.initial_password
    body = directory.created[0]
    assert body["password"] == result.initial_password
    assert body["changePasswordAtNextLogin"] is True
    assert body["orgUnitPath"] == "/Staff"
    assert body["recoveryEmail"] == "ada.private@example.invalid"
    assert body["name"] == {"givenName": "Ada", "familyName": "Lovelace"}
    assert sorted(result.groups_added) == ["g1", "g2"]
    assert directory.memberships == {"g1": {"new1"}, "g2": {"new1"}}


def test_no_password_is_returned_when_delivery_is_off(mock_transport):
    directory = Directory()
    adapter = make_adapter(mock_transport(directory), deliver_initial_password=False)
    result = adapter.ensure_account(spec(), groups=set(), managed_groups=set())
    assert result.created
    assert result.initial_password is None
    assert directory.created[0]["password"]


def test_email_outside_domain_is_refused(mock_transport):
    adapter = make_adapter(mock_transport(Directory()))
    with pytest.raises(AdapterError, match="example.invalid"):
        adapter.ensure_account(
            spec(email="ada@elsewhere.example.invalid"), groups=set(), managed_groups=set()
        )


def test_existing_user_on_conflict_gets_no_password(mock_transport):
    directory = Directory([user()])
    adapter = make_adapter(mock_transport(directory))
    # The create conflicts with an account made after the app last looked
    # (e.g. a create whose response was lost); the adapter reads it instead.
    result = adapter.create_account(spec())
    assert not result.created
    assert result.initial_password is None
    assert result.notes


def test_ensure_reactivates_suspended_user(mock_transport):
    directory = Directory([user(suspended=True)])
    adapter = make_adapter(mock_transport(directory))
    result = adapter.ensure_account(spec(), external_id="u1", groups=set(), managed_groups=set())
    assert result.reactivated
    assert not result.created
    assert result.account.active
    assert directory.users["u1"]["suspended"] is False


def test_suspended_admin_is_not_reactivated(mock_transport):
    directory = Directory([user(suspended=True, admin=True)])
    adapter = make_adapter(mock_transport(directory))
    with pytest.raises(AdapterError, match="administrator"):
        adapter.ensure_account(spec(), external_id="u1", groups=set(), managed_groups=set())
    assert directory.users["u1"]["suspended"] is True


def test_disable_suspends_and_removes_only_managed_groups(mock_transport):
    directory = Directory(
        [user()], memberships={"managed": {"u1"}, "unmanaged": {"u1"}, "other": set()}
    )
    adapter = make_adapter(mock_transport(directory))
    result = adapter.disable_account(spec(), external_id="u1", managed_groups={"managed", "other"})
    assert result.changed
    assert result.groups_removed == ["managed"]
    assert directory.memberships["unmanaged"] == {"u1"}
    assert directory.memberships["managed"] == set()
    assert directory.users["u1"]["suspended"] is True
    assert not result.account.active


def test_disable_without_account_does_nothing(mock_transport):
    adapter = make_adapter(mock_transport(Directory()))
    result = adapter.disable_account(spec(), managed_groups={"g1"})
    assert result.account is None
    assert not result.changed


def test_super_admin_is_not_suspended(mock_transport):
    directory = Directory([user(admin=True)])
    adapter = make_adapter(mock_transport(directory))
    with pytest.raises(AdapterError, match="super admins"):
        adapter.disable_account(spec(), external_id="u1", managed_groups=set())
    assert directory.users["u1"]["suspended"] is False


def test_duplicate_member_conflict_is_ignored(mock_transport):
    directory = Directory([user()])
    directory.member_status = 409
    adapter = make_adapter(mock_transport(directory))
    result = adapter.ensure_account(spec(), external_id="u1", groups={"g1"}, managed_groups={"g1"})
    assert result.groups_added == ["g1"]


def test_rate_limit_is_transient(mock_transport):
    adapter = make_adapter(mock_transport(lambda r: httpx.Response(429, json={})))
    with pytest.raises(TransientError):
        adapter.list_groups()


def test_forbidden_is_a_configuration_error(mock_transport):
    adapter = make_adapter(mock_transport(lambda r: httpx.Response(403, text="secret body")))
    with pytest.raises(ConfigurationError) as excinfo:
        adapter.test_connection()
    assert "secret body" not in str(excinfo.value)


def test_list_accounts_follows_pages(mock_transport):
    pages = {
        None: {"users": [user("u1")], "nextPageToken": "n"},
        "n": {"users": [user("u2", "bob@example.invalid", suspended=True)]},
    }
    transport = mock_transport(
        lambda r: httpx.Response(200, json=pages[r.url.params.get("pageToken")])
    )
    accounts = list(make_adapter(transport).list_accounts())
    assert [(a.external_id, a.email, a.active, a.groups) for a in accounts] == [
        ("u1", "ada@example.invalid", True, None),
        ("u2", "bob@example.invalid", False, None),
    ]
    assert transport.requests[0].url.params["maxResults"] == "500"

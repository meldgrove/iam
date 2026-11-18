import json
from urllib.parse import parse_qs

import httpx
import pytest

from iam.adapters.base import AccountSpec, AdapterError, ConfigurationError, TransientError
from iam.adapters.keycloak import KeycloakAdapter

BASE = "https://sso.example.invalid"
REALM = "example"
ADMIN = f"/admin/realms/{REALM}"
TOKEN_PATH = f"/realms/{REALM}/protocol/openid-connect/token"


class FakeKeycloak:
    """Just enough of the Keycloak Admin REST API for the adapter."""

    def __init__(self):
        self.users: dict[str, dict] = {}
        self.memberships: dict[str, set[str]] = {}
        self.groups: dict[str, dict] = {}
        self.token_status = 200
        self.token_calls = 0
        self.failures: dict[tuple[str, str], int] = {}
        self.calls: list[tuple[str, str, dict, object]] = []
        self.inline_subgroups = False
        self.default_groups: set[str] = set()
        self._next = 0

    # -- setup helpers --

    def add_group(self, group_id, name, parent=None):
        parent_path = self.groups[parent]["path"] if parent else ""
        self.groups[group_id] = {
            "id": group_id,
            "name": name,
            "path": f"{parent_path}/{name}",
            "parent": parent,
        }

    def add_user(self, username, email="", enabled=True, groups=(), **extra):
        self._next += 1
        user_id = f"user-{self._next}"
        self.users[user_id] = {
            "id": user_id,
            "username": username,
            "email": email,
            "firstName": extra.pop("firstName", "Ada"),
            "lastName": extra.pop("lastName", "Lovelace"),
            "enabled": enabled,
            "access": {"manage": True},
            "attributes": {"department": ["Research"]},
            **extra,
        }
        self.memberships[user_id] = set(groups)
        return user_id

    # -- transport --

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        method = request.method
        params = dict(request.url.params)
        body = json.loads(request.content) if request.content and path != TOKEN_PATH else None
        self.calls.append((method, path, params, body))
        if (method, path) in self.failures:
            return httpx.Response(self.failures[(method, path)], json={"error": "secret-ish"})
        if path == TOKEN_PATH:
            self.token_calls += 1
            form = parse_qs(request.content.decode())
            assert form["grant_type"] == ["client_credentials"]
            if self.token_status != 200:
                return httpx.Response(self.token_status, json={"error": "unauthorized_client"})
            return httpx.Response(200, json={"access_token": "tok", "expires_in": 300})
        assert request.headers["Authorization"] == "Bearer tok"
        assert path.startswith(ADMIN)
        return self.route(method, path[len(ADMIN) :], params, body)

    def route(self, method, path, params, body):
        parts = [p for p in path.split("/") if p]
        if parts == ["users", "count"]:
            return httpx.Response(200, json=len(self.users))
        if parts == ["users"] and method == "GET":
            users = list(self.users.values())
            for field in ("username", "email"):
                if field in params:
                    users = [u for u in users if u[field].lower() == params[field].lower()]
            first, size = int(params.get("first", 0)), int(params.get("max", 100))
            return httpx.Response(200, json=users[first : first + size])
        if parts == ["users"] and method == "POST":
            if any(u["username"] == body["username"] for u in self.users.values()):
                return httpx.Response(409, json={"errorMessage": "User exists"})
            user_id = self.add_user(
                body["username"],
                body.get("email", ""),
                **{"firstName": body["firstName"], "lastName": body["lastName"]},
            )
            self.users[user_id]["enabled"] = body["enabled"]
            self.memberships[user_id] |= self.default_groups
            return httpx.Response(201, headers={"Location": f"{BASE}{ADMIN}/users/{user_id}"})
        if parts[:1] == ["users"] and len(parts) >= 2:
            user_id = parts[1]
            if user_id not in self.users:
                return httpx.Response(404)
            rest = parts[2:]
            if not rest and method == "GET":
                return httpx.Response(200, json=self.users[user_id])
            if not rest and method == "PUT":
                assert "access" not in body
                self.users[user_id].update(body)
                return httpx.Response(204)
            if rest == ["logout"] and method == "POST":
                return httpx.Response(204)
            if rest in (["execute-actions-email"], ["reset-password"]) and method == "PUT":
                return httpx.Response(204)
            if rest == ["groups"] and method == "GET":
                ids = sorted(self.memberships[user_id])
                first, size = int(params.get("first", 0)), int(params.get("max", 100))
                page = [
                    {"id": g, "name": self.groups[g]["name"], "path": self.groups[g]["path"]}
                    for g in ids[first : first + size]
                ]
                return httpx.Response(200, json=page)
            if rest[:1] == ["groups"] and len(rest) == 2:
                group_id = rest[1]
                if group_id not in self.groups:
                    return httpx.Response(404)
                if method == "PUT":
                    self.memberships[user_id].add(group_id)
                else:
                    self.memberships[user_id].discard(group_id)
                return httpx.Response(204)
        if parts == ["groups"]:
            return httpx.Response(200, json=self.group_page(None, params))
        if parts[:1] == ["groups"] and parts[2:] == ["children"]:
            return httpx.Response(200, json=self.group_page(parts[1], params))
        return httpx.Response(404)

    def group_rep(self, group):
        children = [g for g in self.groups.values() if g["parent"] == group["id"]]
        rep = {"id": group["id"], "name": group["name"], "path": group["path"]}
        if self.inline_subgroups:
            rep["subGroups"] = [self.group_rep(child) for child in children]
        else:
            rep["subGroupCount"] = len(children)
            rep["subGroups"] = []
        return rep

    def group_page(self, parent, params):
        groups = [self.group_rep(g) for g in self.groups.values() if g["parent"] == parent]
        first, size = int(params.get("first", 0)), int(params.get("max", 100))
        return groups[first : first + size]

    def calls_to(self, method, suffix):
        return [c for c in self.calls if c[0] == method and c[1].endswith(suffix)]


@pytest.fixture
def keycloak():
    fake = FakeKeycloak()
    fake.add_group("g-apps", "apps")
    fake.add_group("g-wiki", "wiki", parent="g-apps")
    fake.add_group("g-editors", "editors", parent="g-wiki")
    fake.add_group("g-teams", "teams")
    fake.add_group("g-sales", "sales", parent="g-teams")
    fake.add_group("g-other", "other")
    return fake


def adapter_for(fake, mock_transport, **config):
    values = {
        "base_url": BASE,
        "realm": REALM,
        "client_id": "iam",
        "client_secret": "client-secret-value",
        **config,
    }
    return KeycloakAdapter(values, transport=mock_transport(fake))


def spec(**overrides):
    values = {
        "first_name": "Ada",
        "last_name": "Lovelace",
        "username": "ada",
        "email": "ada@example.invalid",
    }
    values.update(overrides)
    return AccountSpec(**values)


def test_connection_reports_user_count(keycloak, mock_transport):
    keycloak.add_user("ada", "ada@example.invalid")
    keycloak.add_user("bob", "bob@example.invalid")
    result = adapter_for(keycloak, mock_transport).test_connection()
    assert result.ok
    assert "2 users" in result.message
    assert keycloak.token_calls == 1


def test_connection_names_missing_roles_on_403(keycloak, mock_transport):
    keycloak.failures[("GET", f"{ADMIN}/users/count")] = 403
    result = adapter_for(keycloak, mock_transport).test_connection()
    assert not result.ok
    assert "403" in result.message
    assert "view-users" in result.message
    assert "secret-ish" not in result.message


def test_connection_names_the_admin_api_scope_on_401(keycloak, mock_transport):
    # Keycloak 26.8.2 and later: a token without the kc-admin-api audience.
    keycloak.failures[("GET", f"{ADMIN}/users/count")] = 401
    result = adapter_for(keycloak, mock_transport).test_connection()
    assert not result.ok
    assert "401" in result.message and "kc-admin-api" in result.message
    assert "client-secret-value" not in result.message


def test_connection_reports_rejected_client_secret(keycloak, mock_transport):
    keycloak.token_status = 401
    result = adapter_for(keycloak, mock_transport).test_connection()
    assert not result.ok
    assert "refused the client ID and secret" in result.message
    assert "client-secret-value" not in result.message


def test_missing_settings_are_a_configuration_error(keycloak, mock_transport):
    adapter = adapter_for(keycloak, mock_transport, client_secret="")
    result = adapter.test_connection()
    assert not result.ok
    assert "Client secret" in result.message


def test_plain_http_is_refused_outside_development(keycloak, mock_transport, settings):
    settings.DEBUG = False
    adapter = adapter_for(keycloak, mock_transport, base_url="http://sso.example.invalid")
    with pytest.raises(ConfigurationError, match="HTTPS"):
        adapter.list_groups()
    loopback = adapter_for(keycloak, mock_transport, base_url="http://localhost:8080")
    loopback._check_config()


def test_list_groups_flattens_the_tree(keycloak, mock_transport):
    groups = adapter_for(keycloak, mock_transport).list_groups()
    assert {g.path: g.external_id for g in groups} == {
        "/apps": "g-apps",
        "/apps/wiki": "g-wiki",
        "/apps/wiki/editors": "g-editors",
        "/teams": "g-teams",
        "/teams/sales": "g-sales",
        "/other": "g-other",
    }
    # Groups without children are not asked for them.
    assert not keycloak.calls_to("GET", "/groups/g-other/children")


def test_list_groups_filters_by_group_root(keycloak, mock_transport):
    groups = adapter_for(keycloak, mock_transport, group_root="apps/").list_groups()
    assert [g.path for g in groups] == ["/apps/wiki", "/apps/wiki/editors"]


def test_list_groups_reads_inline_subgroups_of_older_versions(keycloak, mock_transport):
    keycloak.inline_subgroups = True
    groups = adapter_for(keycloak, mock_transport).list_groups()
    assert "/apps/wiki/editors" in {g.path for g in groups}
    assert not [c for c in keycloak.calls if c[1].endswith("/children")]


def test_ensure_account_creates_user_sends_setup_email_and_adds_groups(keycloak, mock_transport):
    adapter = adapter_for(keycloak, mock_transport)
    result = adapter.ensure_account(spec(), groups={"g-wiki"}, managed_groups={"g-wiki", "g-sales"})
    assert result.created
    user_id = result.account.external_id
    user = keycloak.users[user_id]
    assert user["username"] == "ada"
    assert user["email"] == "ada@example.invalid"
    assert user["enabled"] is True
    assert keycloak.memberships[user_id] == {"g-wiki"}
    assert result.groups_added == ["g-wiki"]
    (call,) = keycloak.calls_to("PUT", "/execute-actions-email")
    assert call[2]["lifespan"] == str(72 * 3600)
    assert call[3] == ["UPDATE_PASSWORD"]
    assert any("setup email" in note for note in result.notes)
    assert result.initial_password is None
    # Keycloak may add default groups to a new user, so they are read.
    assert keycloak.calls_to("GET", f"/users/{user_id}/groups")
    assert keycloak.token_calls == 1


def test_ensure_account_changes_only_managed_groups(keycloak, mock_transport):
    user_id = keycloak.add_user("ada", "ada@example.invalid", groups={"g-sales", "g-other"})
    result = adapter_for(keycloak, mock_transport).ensure_account(
        spec(), groups={"g-wiki"}, managed_groups={"g-wiki", "g-sales"}
    )
    assert not result.created
    assert result.groups_added == ["g-wiki"]
    assert result.groups_removed == ["g-sales"]
    assert keycloak.memberships[user_id] == {"g-wiki", "g-other"}
    assert not keycloak.calls_to("PUT", "/execute-actions-email")


def test_ensure_account_finds_by_external_id_then_email(keycloak, mock_transport):
    user_id = keycloak.add_user("ada.lovelace", "ada@example.invalid")
    adapter = adapter_for(keycloak, mock_transport)
    by_email = adapter.find_account(spec())
    assert by_email.external_id == user_id
    by_id = adapter.find_account(spec(username="someone-else"), external_id=user_id)
    assert by_id.external_id == user_id
    assert adapter.find_account(spec(username="nobody", email="")) is None


def test_ensure_account_reactivates_a_disabled_user(keycloak, mock_transport):
    user_id = keycloak.add_user("ada", "ada@example.invalid", enabled=False)
    result = adapter_for(keycloak, mock_transport).ensure_account(
        spec(), external_id=user_id, groups=set(), managed_groups=set()
    )
    assert result.reactivated
    assert result.account.active
    user = keycloak.users[user_id]
    assert user["enabled"] is True
    # The whole representation went back, so attributes survive.
    assert user["attributes"] == {"department": ["Research"]}
    assert not keycloak.calls_to("POST", "/logout")


def test_ensure_account_updates_names_but_never_the_email(keycloak, mock_transport):
    user_id = keycloak.add_user("ada", "old@example.invalid", firstName="Augusta")
    result = adapter_for(keycloak, mock_transport).ensure_account(
        spec(), external_id=user_id, groups=set(), managed_groups=set()
    )
    assert keycloak.users[user_id]["firstName"] == "Ada"
    assert keycloak.users[user_id]["email"] == "old@example.invalid"
    assert any("left it unchanged" in note for note in result.notes)


def test_disable_account_removes_managed_groups_disables_and_logs_out(keycloak, mock_transport):
    user_id = keycloak.add_user(
        "ada", "ada@example.invalid", groups={"g-wiki", "g-sales", "g-other"}
    )
    result = adapter_for(keycloak, mock_transport).disable_account(
        spec(), external_id=user_id, managed_groups={"g-wiki", "g-sales"}
    )
    assert result.changed
    assert result.groups_removed == ["g-sales", "g-wiki"]
    assert keycloak.memberships[user_id] == {"g-other"}
    assert keycloak.users[user_id]["enabled"] is False
    assert keycloak.calls_to("POST", f"/users/{user_id}/logout")
    assert not result.account.active


def test_disable_account_without_logout(keycloak, mock_transport):
    user_id = keycloak.add_user("ada", "ada@example.invalid")
    adapter_for(keycloak, mock_transport, logout_on_disable=False).disable_account(
        spec(), external_id=user_id, managed_groups=set()
    )
    assert keycloak.users[user_id]["enabled"] is False
    assert not keycloak.calls_to("POST", "/logout")


def test_disable_account_without_an_account_changes_nothing(keycloak, mock_transport):
    result = adapter_for(keycloak, mock_transport).disable_account(
        spec(), managed_groups={"g-wiki"}
    )
    assert result.account is None
    assert not result.changed


def test_server_errors_are_transient(keycloak, mock_transport):
    keycloak.failures[("GET", f"{ADMIN}/users")] = 503
    with pytest.raises(TransientError) as excinfo:
        adapter_for(keycloak, mock_transport).find_account(spec())
    assert "secret-ish" not in str(excinfo.value)


def test_unreachable_keycloak_is_transient(mock_transport):
    def handler(request):
        raise httpx.ConnectError("refused", request=request)

    adapter = KeycloakAdapter(
        {"base_url": BASE, "realm": REALM, "client_id": "iam", "client_secret": "s"},
        transport=mock_transport(handler),
    )
    with pytest.raises(TransientError):
        adapter.list_groups()


def test_create_conflict_adopts_the_existing_user(keycloak, mock_transport):
    user_id = keycloak.add_user("ada", "ada@example.invalid", enabled=False)
    result = adapter_for(keycloak, mock_transport).create_account(spec())
    assert not result.created
    assert result.account.external_id == user_id
    assert result.reactivated
    assert keycloak.users[user_id]["enabled"] is True
    assert any("already had" in note for note in result.notes)
    assert not keycloak.calls_to("PUT", "/execute-actions-email")


def test_create_conflict_with_a_different_email_fails(keycloak, mock_transport):
    keycloak.add_user("ada", "someone@example.invalid")
    with pytest.raises(AdapterError, match="different email"):
        adapter_for(keycloak, mock_transport).create_account(spec())


def test_temporary_password_is_returned_for_delivery(keycloak, mock_transport):
    adapter = adapter_for(
        keycloak, mock_transport, send_setup_email=False, set_temporary_password=True
    )
    result = adapter.create_account(spec())
    assert result.initial_password
    (call,) = keycloak.calls_to("PUT", "/reset-password")
    assert call[3]["temporary"] is True
    assert call[3]["value"] == result.initial_password


def test_failed_setup_email_becomes_a_note(keycloak, mock_transport):
    adapter = adapter_for(keycloak, mock_transport)
    # Fail the setup email for the user that will be created next.
    keycloak.failures[("PUT", f"{ADMIN}/users/user-1/execute-actions-email")] = 500
    result = adapter.create_account(spec())
    assert result.created
    assert any("could not send the setup email (500)" in note for note in result.notes)


def test_username_from_full_email(keycloak, mock_transport):
    adapter = adapter_for(keycloak, mock_transport, username_source="email")
    result = adapter.create_account(spec(email="Ada@Example.invalid"))
    assert keycloak.users[result.account.external_id]["username"] == "ada@example.invalid"


def test_list_accounts_pages_and_skips_service_accounts(keycloak, mock_transport):
    for index in range(150):
        keycloak.add_user(f"user{index}", f"user{index}@example.invalid")
    keycloak.add_user("service-account-iam", serviceAccountClientLink="iam")
    accounts = list(adapter_for(keycloak, mock_transport).list_accounts())
    assert len(accounts) == 150
    assert all(account.groups is None for account in accounts)
    assert len(keycloak.calls_to("GET", f"{ADMIN}/users")) == 2


def test_cached_token_is_renewed_once_after_401(keycloak, mock_transport):
    adapter = adapter_for(keycloak, mock_transport)
    adapter.list_groups()
    keycloak.failures[("GET", f"{ADMIN}/users/count")] = 401
    # The retry also gets 401: the second answer is reported, no loop.
    with pytest.raises(ConfigurationError):
        adapter._request("GET", f"admin/realms/{REALM}/users/count", "count users")
    assert keycloak.token_calls == 2


def test_dot_segments_are_refused(keycloak, mock_transport):
    with pytest.raises(AdapterError):
        adapter_for(keycloak, mock_transport).find_account(spec(), external_id="..")


def test_manual_instructions_mention_the_console(keycloak, mock_transport):
    adapter = adapter_for(keycloak, mock_transport)
    text = adapter.manual_instructions("ensure_account", spec(), ["/apps/wiki"])
    assert "Keycloak admin console" in text
    assert "/apps/wiki" in text
    assert "turn Enabled off" in adapter.manual_instructions("disable_account", spec(), [])


def test_plain_http_needs_explicit_opt_in(settings):
    from iam.adapters.base import ConfigurationError
    from iam.adapters.keycloak import KeycloakAdapter

    settings.DEBUG = False
    config = {
        "base_url": "http://keycloak:8080",
        "realm": "example",
        "client_id": "iam",
        "client_secret": "secret",
    }
    try:
        KeycloakAdapter(config)._check_config()
    except ConfigurationError as exc:
        assert "HTTPS" in str(exc)
    else:
        raise AssertionError("plain HTTP was accepted without opt-in")
    KeycloakAdapter({**config, "allow_http": True})._check_config()


def test_a_username_of_someone_with_another_email_is_never_changed(keycloak, mock_transport):
    other = keycloak.add_user("ada", "ada.byron@example.invalid", enabled=False)
    with pytest.raises(AdapterError, match="different email"):
        adapter_for(keycloak, mock_transport).ensure_account(
            spec(), groups={"g-wiki"}, managed_groups={"g-wiki"}
        )
    with pytest.raises(AdapterError, match="different email"):
        adapter_for(keycloak, mock_transport).disable_account(spec(), managed_groups=set())
    assert not keycloak.calls_to("PUT", f"/users/{other}")
    assert keycloak.memberships[other] == set()


def test_a_managed_default_group_leaves_a_new_user(keycloak, mock_transport):
    keycloak.default_groups = {"g-sales"}
    result = adapter_for(keycloak, mock_transport).ensure_account(
        spec(), groups={"g-wiki"}, managed_groups={"g-wiki", "g-sales"}
    )
    assert keycloak.memberships[result.account.external_id] == {"g-wiki"}
    assert result.groups_removed == ["g-sales"]


def test_a_failed_logout_runs_again_with_the_job(keycloak, mock_transport):
    user_id = keycloak.add_user("ada", "ada@example.invalid")
    keycloak.failures[("POST", f"{ADMIN}/users/{user_id}/logout")] = 503
    with pytest.raises(TransientError):
        adapter_for(keycloak, mock_transport).disable_account(spec(), managed_groups=set())
    assert keycloak.users[user_id]["enabled"] is False
    del keycloak.failures[("POST", f"{ADMIN}/users/{user_id}/logout")]
    result = adapter_for(keycloak, mock_transport).disable_account(spec(), managed_groups=set())
    assert not result.changed
    assert len(keycloak.calls_to("POST", f"/users/{user_id}/logout")) == 2


def test_a_setup_email_that_times_out_becomes_a_credentials_problem(keycloak, mock_transport):
    def handler(request):
        if request.url.path.endswith("/execute-actions-email"):
            raise httpx.ReadTimeout("slow", request=request)
        return keycloak(request)

    adapter = KeycloakAdapter(
        {"base_url": BASE, "realm": REALM, "client_id": "iam", "client_secret": "s"},
        transport=mock_transport(handler),
    )
    result = adapter.ensure_account(spec(), groups=set(), managed_groups=set())
    assert result.created
    assert "could not send the setup email (no answer)" in result.credentials_problem


def test_a_refused_temporary_password_becomes_a_credentials_problem(keycloak, mock_transport):
    keycloak.failures[("PUT", f"{ADMIN}/users/user-1/reset-password")] = 400
    result = adapter_for(
        keycloak, mock_transport, send_setup_email=False, set_temporary_password=True
    ).ensure_account(spec(), groups=set(), managed_groups=set())
    assert result.initial_password is None
    assert "did not set a temporary password (400)" in result.credentials_problem


def test_keycloak_hands_out_sign_in_unless_both_ways_are_off(keycloak, mock_transport):
    assert adapter_for(keycloak, mock_transport).hands_out_sign_in()
    assert adapter_for(
        keycloak, mock_transport, send_setup_email=False, set_temporary_password=True
    ).hands_out_sign_in()
    assert not adapter_for(
        keycloak, mock_transport, send_setup_email=False, set_temporary_password=False
    ).hands_out_sign_in()

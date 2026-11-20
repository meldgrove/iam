import json

import httpx
import pytest

from iam.adapters.base import AccountSpec, AdapterError, ConfigurationError, TransientError
from iam.adapters.relution import USERS_PATH, RelutionAdapter, update_payload

from .rehearse import rehearse

ORG = "org-1"
CONFIG = {
    "base_url": "https://relution.example.invalid",
    "organization_uuid": ORG,
    "api_token": "relution-test-token",
}
SPEC = AccountSpec(
    first_name="Ada", last_name="Lovelace", username="ada", email="Ada@Example.invalid"
)


class FakeRelution:
    """In-memory Relution: users, groups and the per-user membership resource."""

    def __init__(self):
        self.users = {}
        self.groups = {
            "g-1": {"uuid": "g-1", "name": "Staff devices", "organizationUuid": ORG},
            "g-2": {"uuid": "g-2", "name": "Laptops", "organizationUuid": ORG},
            "g-x": {"uuid": "g-x", "name": "Other org", "organizationUuid": "org-2"},
        }
        self.members = {}
        self.fail_with = None
        self.writes = []

    def add_user(self, uuid, email, activated=True, org=ORG, **extra):
        self.users[uuid] = {
            "uuid": uuid,
            "organizationUuid": org,
            "name": email,
            "email": email,
            "givenName": "Ada",
            "surname": "Lovelace",
            "activated": activated,
            "locked": False,
            "ssoOnly": True,
            **extra,
        }
        self.members[uuid] = set()

    def __call__(self, request: httpx.Request) -> httpx.Response:
        assert request.headers["X-User-Access-Token"] == "relution-test-token"
        if self.fail_with:
            return httpx.Response(self.fail_with, json={"error": "secret detail"})
        path = request.url.path
        method = request.method
        if method != "GET":
            self.writes.append((method, path))
        if path == USERS_PATH and method == "GET":
            return self.page(list(self.users.values()), request)
        if path == "/api/management/v1/security/groups" and method == "GET":
            return self.page(list(self.groups.values()), request)
        if path == USERS_PATH and method == "POST":
            body = json.loads(request.content)
            assert body["organizationUuid"] == ORG
            uuid = f"u-{len(self.users) + 1}"
            self.users[uuid] = {"uuid": uuid, **body, "locked": False}
            self.members[uuid] = set()
            return httpx.Response(201, json=self.users[uuid])
        prefix = USERS_PATH + "/"
        if path.startswith(prefix):
            rest = path[len(prefix) :]
            uuid, _, sub = rest.partition("/")
            if uuid not in self.users:
                return httpx.Response(404, json={})
            if sub == "groups" and method == "GET":
                return httpx.Response(
                    200, json={"groups": [{"uuid": g} for g in sorted(self.members[uuid])]}
                )
            if sub == "groups" and method == "PUT":
                body = json.loads(request.content)
                assert isinstance(body, list)
                self.members[uuid] = set(body)
                return httpx.Response(204)
            if method == "GET":
                return httpx.Response(200, json=self.users[uuid])
            if method == "PUT":
                body = json.loads(request.content)
                assert not any("password" in key.lower() for key in body)
                self.users[uuid] = body
                return httpx.Response(200, json=body)
        return httpx.Response(405)

    def page(self, items, request):
        limit = int(request.url.params["limit"])
        offset = int(request.url.params["offset"])
        return httpx.Response(200, json={"items": items[offset : offset + limit]})


@pytest.fixture
def fake():
    return FakeRelution()


@pytest.fixture
def adapter(fake, mock_transport):
    return RelutionAdapter(CONFIG, transport=mock_transport(fake))


def test_test_connection_ok(adapter):
    result = adapter.test_connection()
    assert result.ok


def test_test_connection_unauthorized_hides_body(fake, adapter):
    fake.fail_with = 401
    with pytest.raises(ConfigurationError) as exc:
        adapter.test_connection()
    assert "secret detail" not in str(exc.value)
    assert "relution-test-token" not in str(exc.value)


def test_server_error_is_transient(fake, adapter):
    fake.fail_with = 503
    with pytest.raises(TransientError):
        adapter.list_groups()


def test_rejects_address_with_path_or_http(mock_transport, fake):
    for url in ("https://relution.example.invalid/api", "http://relution.example.invalid"):
        bad = RelutionAdapter({**CONFIG, "base_url": url}, transport=mock_transport(fake))
        with pytest.raises(ConfigurationError):
            bad.test_connection()


def test_missing_token_is_configuration_error(mock_transport, fake):
    bad = RelutionAdapter({**CONFIG, "api_token": ""}, transport=mock_transport(fake))
    with pytest.raises(ConfigurationError):
        bad.test_connection()


def test_list_groups_ignores_other_organizations(adapter):
    groups = adapter.list_groups()
    assert [g.external_id for g in groups] == ["g-1", "g-2"]
    assert groups[0].name == "Staff devices"


def test_create_account_with_groups(fake, adapter):
    result = adapter.ensure_account(SPEC, groups={"g-1"}, managed_groups={"g-1", "g-2"})
    assert result.created
    assert result.groups_added == ["g-1"]
    user = fake.users[result.account.external_id]
    assert user["email"] == "ada@example.invalid"
    assert user["managedAppleId"] == "ada@example.invalid"
    assert user["ssoOnly"] is True
    assert fake.members[result.account.external_id] == {"g-1"}
    assert result.initial_password is None


def test_create_without_work_email_fails(adapter):
    spec = AccountSpec(first_name="Ada", last_name="Lovelace", username="ada")
    with pytest.raises(AdapterError):
        adapter.create_account(spec)


def test_find_by_id_then_email(fake, adapter):
    fake.add_user("u-9", "ada@example.invalid")
    by_id = adapter.find_account(SPEC, "u-9")
    assert by_id.external_id == "u-9"
    # A stale ID falls back to the work email.
    by_email = adapter.find_account(SPEC, "gone")
    assert by_email.external_id == "u-9"
    assert adapter.find_account(AccountSpec("X", "Y", "nobody", email="x@example.invalid")) is None


def test_find_ignores_other_organizations(fake, adapter):
    fake.add_user("u-other", "ada@example.invalid", org="org-2")
    assert adapter.find_account(SPEC) is None


def test_duplicate_matches_are_refused(fake, adapter):
    fake.add_user("u-1", "ada@example.invalid")
    fake.add_user("u-2", "ADA@example.invalid")
    with pytest.raises(AdapterError):
        adapter.find_account(SPEC)


def test_ensure_existing_reactivates_and_reconciles_managed_groups(fake, adapter):
    fake.add_user("u-1", "ada@example.invalid", activated=False)
    fake.members["u-1"] = {"g-2", "unmanaged"}
    result = adapter.ensure_account(
        SPEC, external_id="u-1", groups={"g-1"}, managed_groups={"g-1", "g-2"}
    )
    assert not result.created
    assert result.reactivated
    assert result.groups_added == ["g-1"]
    assert result.groups_removed == ["g-2"]
    assert fake.users["u-1"]["activated"] is True
    assert fake.members["u-1"] == {"g-1", "unmanaged"}


def test_locked_user_is_not_reported_active(fake, adapter):
    fake.add_user("u-1", "ada@example.invalid", activated=False, locked=True)

    def keep_locked(request):
        response = fake(request)
        fake.users["u-1"]["locked"] = True
        return response

    locked = RelutionAdapter(CONFIG, transport=httpx.MockTransport(keep_locked))
    with pytest.raises(AdapterError, match="locked"):
        locked.ensure_account(SPEC, external_id="u-1", groups=set(), managed_groups=set())


def test_disable_deactivates_without_delete_and_keeps_groups(fake, adapter):
    fake.add_user("u-1", "ada@example.invalid", password="hash")
    fake.members["u-1"] = {"g-1"}
    result = adapter.disable_account(SPEC, external_id="u-1", managed_groups={"g-1"})
    assert result.changed
    assert result.account.active is False
    assert fake.users["u-1"]["activated"] is False
    assert "password" not in fake.users["u-1"]
    assert fake.members["u-1"] == {"g-1"}
    assert not any(method == "DELETE" for method, _ in fake.writes)


def test_disable_missing_account_is_noop(adapter):
    result = adapter.disable_account(SPEC, managed_groups=set())
    assert result.account is None
    assert not result.changed


def test_disable_already_inactive_writes_nothing(fake, adapter):
    fake.add_user("u-1", "ada@example.invalid", activated=False)
    result = adapter.disable_account(SPEC, external_id="u-1", managed_groups=set())
    assert not result.changed
    assert fake.writes == []


def test_list_accounts_pages(fake, adapter):
    for i in range(205):
        fake.add_user(f"u-{i}", f"user{i}@example.invalid", activated=i % 2 == 0)
    accounts = list(adapter.list_accounts())
    assert len(accounts) == 205
    assert accounts[1].active is False
    assert accounts[0].groups is None


def test_create_after_lost_response_reads_before_failing(fake, mock_transport):
    def flaky(request):
        if request.method == "POST":
            fake(request)  # stored, but the answer is lost
            return httpx.Response(504)
        return fake(request)

    adapter = RelutionAdapter(CONFIG, transport=mock_transport(flaky))
    result = adapter.create_account(SPEC)
    assert result.created
    assert len(fake.users) == 1


def test_update_payload_drops_credentials():
    body = update_payload(
        {"uuid": "u", "passwordHash": "x", "apiToken": "y", "name": "n"}, {"activated": False}
    )
    assert body == {"uuid": "u", "name": "n", "activated": False}


def test_manual_instructions_never_suggest_delete(adapter):
    text = adapter.manual_instructions("disable_account", SPEC, [])
    assert "deactivate" in text
    assert "Do not delete" in text


def test_offboarding_deactivates_a_locked_user(fake, adapter):
    fake.add_user("u-1", "ada@example.invalid", locked=True)
    result = adapter.disable_account(SPEC, external_id="u-1", managed_groups=set())
    assert result.changed
    assert fake.users["u-1"]["activated"] is False


def test_roles_are_not_group_memberships(fake, adapter):
    fake.add_user("u-1", "ada@example.invalid", roles=[{"uuid": "role-admin"}])
    fake.members["u-1"] = {"g-2"}
    result = adapter.ensure_account(
        SPEC, external_id="u-1", groups={"g-1"}, managed_groups={"g-1", "g-2"}
    )
    assert fake.members["u-1"] == {"g-1"}
    assert result.groups_removed == ["g-2"]


SOMEONE_ELSES = (
    "The Relution username {} belongs to a user with a different email. Nothing was "
    "changed. Check the person's username and work email."
)


@pytest.mark.parametrize("name", ["ada", "ada@example.invalid"])
def test_a_username_of_someone_with_another_email_is_never_changed(fake, adapter, name):
    """As at Keycloak: a login name can belong to someone else (another
    J. Smith), and an email that is not the person's shows it does."""
    fake.add_user("u-7", "ada.byron@example.invalid", activated=False)
    fake.users["u-7"]["name"] = name
    with pytest.raises(AdapterError) as exc:
        adapter.ensure_account(SPEC, groups={"g-1"}, managed_groups={"g-1"})
    assert str(exc.value) == SOMEONE_ELSES.format(name)
    with pytest.raises(AdapterError) as exc:
        adapter.disable_account(SPEC, managed_groups={"g-1"})
    assert str(exc.value) == SOMEONE_ELSES.format(name)
    assert fake.writes == []


def test_a_create_whose_answer_was_lost_never_takes_over_someone_elses_username(
    fake, mock_transport
):
    def flaky(request):
        if request.method == "POST":
            # Someone else's user holds the login name meanwhile.
            fake.add_user("u-7", "ada.byron@example.invalid")
            fake.users["u-7"]["name"] = "ada@example.invalid"
            return httpx.Response(504)
        return fake(request)

    adapter = RelutionAdapter(CONFIG, transport=mock_transport(flaky))
    with pytest.raises(AdapterError) as exc:
        adapter.create_account(SPEC)
    assert str(exc.value) == SOMEONE_ELSES.format("ada@example.invalid")


def test_a_username_match_without_an_email_is_theirs(fake, adapter):
    fake.add_user("u-1", "")
    fake.users["u-1"]["name"] = "ada"
    assert adapter.find_account(SPEC).external_id == "u-1"


# -- rehearsals predict the job ------------------------------------------------


def test_a_rehearsal_deactivates_a_locked_user_as_offboarding_does(fake, adapter):
    """A locked user counts as inactive but stays activated: unlocking it
    would let the person in again, so offboarding deactivates it."""
    fake.add_user("u-1", "ada@example.invalid", locked=True)
    plan = rehearse(adapter, "disable_account", SPEC, external_id="u-1")
    assert (plan.change, plan.refusal) == ("disable", "")
    assert fake.writes == []
    result = adapter.disable_account(SPEC, external_id="u-1", managed_groups=set())
    assert result.changed
    assert fake.users["u-1"]["activated"] is False


@pytest.mark.parametrize("activated", [True, False])
def test_a_locked_user_is_not_reactivated(fake, adapter, activated):
    fake.add_user("u-1", "ada@example.invalid", activated=activated, locked=True)
    plan = rehearse(adapter, "ensure_account", SPEC, external_id="u-1", groups={"g-1"})
    assert plan.refusal == (
        "The Relution user is locked. Nothing was changed. Unlock it in Relution, then "
        "run the job again."
    )
    with pytest.raises(AdapterError) as exc:
        adapter.ensure_account(SPEC, external_id="u-1", groups={"g-1"}, managed_groups={"g-1"})
    assert str(exc.value) == plan.refusal
    assert fake.writes == []


def test_a_rehearsal_refuses_a_create_without_a_work_email(fake, adapter):
    spec = AccountSpec(first_name="Ada", last_name="Lovelace", username="ada")
    plan = rehearse(adapter, "ensure_account", spec)
    assert (plan.change, plan.refusal) == (
        "create",
        "A Relution account needs the person's work email.",
    )
    with pytest.raises(AdapterError) as exc:
        adapter.ensure_account(spec, groups=set(), managed_groups=set())
    assert str(exc.value) == plan.refusal


def test_a_rehearsal_lists_the_groups_the_job_changes(fake, adapter):
    fake.add_user("u-1", "ada@example.invalid", activated=False)
    fake.members["u-1"] = {"g-2", "unmanaged"}
    plan = rehearse(adapter, "ensure_account", SPEC, groups={"g-1"}, managed_groups={"g-1", "g-2"})
    assert (plan.change, plan.groups_to_add, plan.groups_to_remove) == (
        "reactivate",
        ["g-1"],
        ["g-2"],
    )
    assert fake.writes == []
    result = adapter.ensure_account(SPEC, groups={"g-1"}, managed_groups={"g-1", "g-2"})
    assert result.reactivated
    assert (result.groups_added, result.groups_removed) == (["g-1"], ["g-2"])

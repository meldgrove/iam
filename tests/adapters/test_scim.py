import json

import httpx
import pytest

from iam.adapters.base import AccountSpec, AdapterError, TransientError
from iam.adapters.scim import ScimAdapter

BASE = "https://app.example.invalid/scim/v2"
TOKEN = "scim-test-token-value"


def spec(**overrides):
    values = {
        "first_name": "Ada",
        "last_name": "Lovelace",
        "username": "ada",
        "email": "ada@example.invalid",
    }
    values.update(overrides)
    return AccountSpec(**values)


def adapter(transport, **config):
    return ScimAdapter({"base_url": BASE, "token": TOKEN, **config}, transport=transport)


def user(uid="u1", active=True, groups=None, user_name="ada@example.invalid"):
    resource = {
        "schemas": ["urn:ietf:params:scim:schemas:core:2.0:User"],
        "id": uid,
        "userName": user_name,
        "displayName": "Ada Lovelace",
        "emails": [{"value": "ada@example.invalid", "primary": True}],
        "active": active,
    }
    if groups is not None:
        resource["groups"] = [{"value": g} for g in groups]
    return resource


def listing(*resources, total=None):
    return {
        "schemas": ["urn:ietf:params:scim:api:messages:2.0:ListResponse"],
        "totalResults": len(resources) if total is None else total,
        "itemsPerPage": len(resources),
        "startIndex": 1,
        "Resources": list(resources),
    }


def body(request):
    return json.loads(request.content)


def test_connection_ok_uses_bearer_token(mock_transport):
    def handler(request):
        assert request.headers["Authorization"] == f"Bearer {TOKEN}"
        if request.url.path.endswith("/ServiceProviderConfig"):
            return httpx.Response(200, json={"patch": {"supported": True}})
        assert request.url.path == "/scim/v2/Users"
        assert request.url.params["count"] == "1"
        return httpx.Response(200, json=listing())

    transport = mock_transport(handler)
    result = adapter(transport).test_connection()
    assert result.ok
    assert len(transport.requests) == 2


def test_connection_refused_reports_without_secret(mock_transport):
    def handler(request):
        if request.url.path.endswith("/ServiceProviderConfig"):
            return httpx.Response(200, json={})
        return httpx.Response(403, text=f"denied for {TOKEN}")

    result = adapter(mock_transport(handler)).test_connection()
    assert not result.ok
    assert "403" in result.message
    assert TOKEN not in result.message


def test_connection_without_service_provider_config(mock_transport):
    def handler(request):
        if request.url.path.endswith("/ServiceProviderConfig"):
            return httpx.Response(404)
        return httpx.Response(200, json=listing())

    assert adapter(mock_transport(handler)).test_connection().ok


def test_find_by_user_name_filter_escapes_quotes(mock_transport):
    def handler(request):
        assert request.url.path == "/scim/v2/Users"
        assert request.url.params["filter"] == 'userName eq "o\\"brien@example.invalid"'
        found = user(groups=["g1", "g2"], user_name='o"brien@example.invalid')
        return httpx.Response(200, json=listing(found))

    account = adapter(mock_transport(handler)).find_account(spec(email='o"brien@example.invalid'))
    assert account.external_id == "u1"
    assert account.email == "ada@example.invalid"
    assert account.active
    assert account.groups == frozenset({"g1", "g2"})


def test_find_by_external_id_first(mock_transport):
    def handler(request):
        assert request.url.path == "/scim/v2/Users/u9"
        return httpx.Response(200, json=user("u9", active=False))

    account = adapter(mock_transport(handler)).find_account(spec(), "u9")
    assert account.external_id == "u9"
    assert not account.active
    assert account.groups is None


def test_find_ignores_unsupported_email_filter(mock_transport):
    def handler(request):
        if request.url.path == "/scim/v2/Users/gone":
            return httpx.Response(404)
        flt = request.url.params["filter"]
        if flt.startswith("userName"):
            assert flt == 'userName eq "ada"'
            return httpx.Response(200, json=listing())
        assert flt == 'emails.value eq "ada@example.invalid"'
        return httpx.Response(400, json={"detail": "unsupported filter"})

    found = adapter(mock_transport(handler), user_name_source="username").find_account(
        spec(), "gone"
    )
    assert found is None


def test_ensure_creates_user_and_adds_groups(mock_transport):
    def handler(request):
        if request.method == "GET" and request.url.path == "/scim/v2/Users":
            return httpx.Response(200, json=listing())
        if request.method == "POST":
            payload = body(request)
            assert payload["userName"] == "ada@example.invalid"
            assert payload["name"] == {"givenName": "Ada", "familyName": "Lovelace"}
            assert payload["emails"] == [
                {"value": "ada@example.invalid", "primary": True, "type": "work"}
            ]
            assert payload["active"] is True
            assert request.headers["Content-Type"] == "application/scim+json"
            return httpx.Response(201, json=user("new"))
        if request.method == "GET" and request.url.path == "/scim/v2/Groups":
            assert request.url.params["filter"] == 'members.value eq "new"'
            return httpx.Response(200, json=listing({"id": "old", "displayName": "Old"}))
        if request.method == "PATCH":
            return httpx.Response(204)
        raise AssertionError(request)

    transport = mock_transport(handler)
    result = adapter(transport).ensure_account(spec(), groups={"g1"}, managed_groups={"g1", "old"})
    assert result.created
    assert result.account.external_id == "new"
    assert result.groups_added == ["g1"]
    assert result.groups_removed == ["old"]
    patches = [r for r in transport.requests if r.method == "PATCH"]
    assert patches[0].url.path == "/scim/v2/Groups/g1"
    assert body(patches[0])["Operations"] == [
        {"op": "add", "path": "members", "value": [{"value": "new"}]}
    ]
    assert patches[1].url.path == "/scim/v2/Groups/old"
    assert body(patches[1])["Operations"] == [{"op": "remove", "path": 'members[value eq "new"]'}]


def test_create_conflict_finds_existing_user(mock_transport):
    calls = {"search": 0}

    def handler(request):
        if request.method == "GET":
            calls["search"] += 1
            if calls["search"] == 1:
                return httpx.Response(200, json=listing())
            return httpx.Response(200, json=listing(user("existing", groups=[])))
        if request.method == "POST":
            return httpx.Response(409, json={"detail": "uniqueness"})
        raise AssertionError(request)

    result = adapter(mock_transport(handler)).ensure_account(
        spec(), groups=set(), managed_groups=set()
    )
    assert not result.created
    assert result.account.external_id == "existing"


def test_disable_deactivates_and_removes_managed_groups(mock_transport):
    def handler(request):
        if request.method == "GET":
            return httpx.Response(200, json=user("u1", groups=["g1", "unmanaged"]))
        if request.method == "PATCH" and request.url.path == "/scim/v2/Groups/g1":
            return httpx.Response(204)
        if request.method == "PATCH" and request.url.path == "/scim/v2/Users/u1":
            assert body(request)["Operations"] == [
                {"op": "replace", "path": "active", "value": False}
            ]
            return httpx.Response(200, json=user("u1", active=False))
        raise AssertionError(request)

    transport = mock_transport(handler)
    result = adapter(transport).disable_account(spec(), external_id="u1", managed_groups={"g1"})
    assert result.changed
    assert not result.account.active
    assert result.groups_removed == ["g1"]
    assert not any(r.url.path.endswith("/unmanaged") for r in transport.requests)


def test_deactivate_falls_back_to_value_form(mock_transport):
    def handler(request):
        if request.method == "GET":
            return httpx.Response(200, json=user("u1"))
        operations = body(request)["Operations"]
        if operations[0].get("path") == "active":
            return httpx.Response(400, json={"detail": "path not supported"})
        assert operations == [{"op": "replace", "value": {"active": False}}]
        return httpx.Response(204)

    result = adapter(mock_transport(handler), groups_enabled=False).disable_account(
        spec(), external_id="u1", managed_groups=set()
    )
    assert result.changed
    assert result.account.active is False


def test_disable_with_delete_mode(mock_transport):
    def handler(request):
        if request.method == "GET":
            return httpx.Response(200, json=user("u1", groups=[]))
        assert request.method == "DELETE"
        assert request.url.path == "/scim/v2/Users/u1"
        return httpx.Response(204)

    result = adapter(mock_transport(handler), deactivate_mode="delete").disable_account(
        spec(), external_id="u1", managed_groups=set()
    )
    assert result.changed
    assert result.account.active is False


def test_server_error_is_transient(mock_transport):
    transport = mock_transport(lambda request: httpx.Response(503))
    with pytest.raises(TransientError):
        adapter(transport).find_account(spec())


def test_client_error_is_permanent(mock_transport):
    transport = mock_transport(lambda request: httpx.Response(422))
    with pytest.raises(AdapterError) as exc:
        adapter(transport).find_account(spec())
    assert not exc.value.retryable


def test_list_groups_pages(mock_transport):
    def handler(request):
        start = int(request.url.params["startIndex"])
        assert request.url.params["attributes"] == "displayName"
        if start == 1:
            return httpx.Response(
                200,
                json=listing(
                    *[{"id": f"g{i}", "displayName": f"Group {i}"} for i in range(100)], total=101
                ),
            )
        assert start == 101
        return httpx.Response(200, json=listing({"id": "g100", "displayName": "Last"}, total=101))

    groups = adapter(mock_transport(handler)).list_groups()
    assert len(groups) == 101
    assert groups[-1].name == "Last"


def test_groups_disabled_skips_group_calls(mock_transport):
    def handler(request):
        if request.method == "GET":
            return httpx.Response(200, json=listing(user("u1")))
        raise AssertionError(request)

    scim = adapter(mock_transport(handler), groups_enabled=False)
    assert scim.list_groups() == []
    result = scim.ensure_account(spec(), groups={"g1"}, managed_groups={"g1"})
    assert result.groups_added == []


def test_list_accounts(mock_transport):
    transport = mock_transport(
        lambda request: httpx.Response(200, json=listing(user("u1"), user("u2", active=False)))
    )
    accounts = list(adapter(transport).list_accounts())
    assert [a.external_id for a in accounts] == ["u1", "u2"]
    assert [a.active for a in accounts] == [True, False]


def test_a_server_that_ignores_the_filter_matches_nobody(mock_transport):
    stranger = user("u7", user_name="grace@example.invalid")
    stranger["emails"] = [{"value": "grace@example.invalid"}]

    def handler(request):
        return httpx.Response(200, json=listing(stranger))

    assert adapter(mock_transport(handler)).find_account(spec()) is None


def test_two_users_with_the_email_are_refused(mock_transport):
    def handler(request):
        if request.url.params["filter"].startswith("userName"):
            return httpx.Response(200, json=listing())
        return httpx.Response(
            200, json=listing(user("u1", user_name="ada"), user("u2", user_name="ada2"))
        )

    with pytest.raises(AdapterError, match="Several"):
        adapter(mock_transport(handler)).find_account(spec())


def test_the_email_finds_a_user_with_another_user_name(mock_transport):
    def handler(request):
        if request.url.params["filter"].startswith("userName"):
            return httpx.Response(200, json=listing())
        assert request.url.params["filter"] == 'emails.value eq "ada@example.invalid"'
        return httpx.Response(200, json=listing(user("u3", user_name="old.name")))

    found = adapter(mock_transport(handler)).find_account(spec())
    assert found.external_id == "u3"


def test_a_listing_that_is_no_list_response_is_an_error(mock_transport):
    def handler(request):
        return httpx.Response(200, text="<html>Maintenance</html>")

    with pytest.raises(AdapterError, match="unreadable"):
        list(adapter(mock_transport(handler)).list_accounts())
    with pytest.raises(AdapterError, match="unreadable"):
        adapter(mock_transport(handler)).list_groups()


def test_group_memberships_are_read_from_every_page(mock_transport):
    def handler(request):
        assert request.url.params["filter"] == 'members.value eq "u1"'
        start = int(request.url.params["startIndex"])
        page = [{"id": f"g{i}"} for i in range(start, min(start + 100, 151))]
        return httpx.Response(200, json=listing(*page, total=150))

    from iam.adapters.base import ExternalAccount

    groups = adapter(mock_transport(handler)).account_groups(ExternalAccount("u1"))
    assert len(groups) == 150 and "g150" in groups


def test_unreadable_group_memberships_are_an_error(mock_transport):
    from iam.adapters.base import ExternalAccount

    def handler(request):
        return httpx.Response(400, json={"detail": "unsupported filter"})

    with pytest.raises(AdapterError, match="cannot list a user's groups"):
        adapter(mock_transport(handler)).account_groups(ExternalAccount("u1"))


def test_a_listing_with_entries_that_are_no_objects_is_an_error(mock_transport):
    def handler(request):
        return httpx.Response(200, json={"Resources": [None], "totalResults": 1})

    with pytest.raises(AdapterError, match="unexpected"):
        list(adapter(mock_transport(handler)).list_accounts())

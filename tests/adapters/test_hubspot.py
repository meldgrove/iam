import json

import httpx
import pytest

from iam.adapters.base import AccountSpec, AdapterError, TransientError
from iam.adapters.hubspot import USERS_PATH, HubSpotAdapter

TOKEN = "hubspot-test-token-value"


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
    return HubSpotAdapter({"access_token": TOKEN, **config}, transport=transport)


def hs_user(uid="11", primary=None, secondary=(), **extra):
    return {
        "id": uid,
        "email": "ada@example.invalid",
        "firstName": "Ada",
        "lastName": "Lovelace",
        "primaryTeamId": primary,
        "secondaryTeamIds": list(secondary),
        **extra,
    }


def body(request):
    return json.loads(request.content)


def raw_path(request):
    return request.url.raw_path.decode().split("?")[0]


def test_connection_ok(mock_transport):
    def handler(request):
        assert request.url.host == "api.hubapi.com"
        assert request.headers["Authorization"] == f"Bearer {TOKEN}"
        if request.url.path == USERS_PATH:
            assert request.url.params["limit"] == "1"
            return httpx.Response(200, json={"results": []})
        assert request.url.path == f"{USERS_PATH}/teams"
        return httpx.Response(200, json={"results": []})

    assert adapter(mock_transport(handler)).test_connection().ok


def test_connection_forbidden(mock_transport):
    transport = mock_transport(lambda request: httpx.Response(403, text=f"bad {TOKEN}"))
    result = adapter(transport).test_connection()
    assert not result.ok
    assert "403" in result.message
    assert TOKEN not in result.message


def test_find_by_id_then_email(mock_transport):
    def handler(request):
        if request.url.path == f"{USERS_PATH}/99":
            return httpx.Response(404)
        if raw_path(request) == f"{USERS_PATH}/ada%40example.invalid":
            assert request.url.params["idProperty"] == "EMAIL"
            return httpx.Response(200, json=hs_user(primary="t1", secondary=["t2"]))
        raise AssertionError(request.url)

    account = adapter(mock_transport(handler)).find_account(spec(), "99")
    assert account.external_id == "11"
    assert account.email == "ada@example.invalid"
    assert account.groups == frozenset({"t1", "t2"})
    assert account.active


def test_missing_user_needs_working_route(mock_transport):
    def handler(request):
        if request.url.path == USERS_PATH:
            return httpx.Response(200, json={"results": []})
        return httpx.Response(404)

    assert adapter(mock_transport(handler)).find_account(spec()) is None


def test_missing_route_is_an_error(mock_transport):
    transport = mock_transport(lambda request: httpx.Response(404))
    with pytest.raises(AdapterError, match="version"):
        adapter(transport).find_account(spec())


def test_ensure_invites_user_and_sets_teams(mock_transport):
    state = {"user": None}

    def handler(request):
        path = request.url.path
        if request.method == "GET" and raw_path(request) == f"{USERS_PATH}/ada%40example.invalid":
            return httpx.Response(404)
        if request.method == "GET" and path == USERS_PATH:
            return httpx.Response(200, json={"results": []})
        if request.method == "POST":
            payload = body(request)
            assert payload == {
                "email": "ada@example.invalid",
                "firstName": "Ada",
                "lastName": "Lovelace",
                "sendWelcomeEmail": False,
                "roleId": "555",
            }
            state["user"] = hs_user()
            return httpx.Response(201, json=state["user"])
        if request.method == "GET" and path == f"{USERS_PATH}/11":
            return httpx.Response(200, json=state["user"])
        if request.method == "PUT":
            payload = body(request)
            state["user"] = hs_user(
                primary=payload["primaryTeamId"], secondary=payload["secondaryTeamIds"]
            )
            return httpx.Response(200, json=state["user"])
        raise AssertionError(request)

    transport = mock_transport(handler)
    result = adapter(transport, role_id="555", send_welcome_email=False).ensure_account(
        spec(), groups={"t1", "t2"}, managed_groups={"t1", "t2"}
    )
    assert result.created
    assert result.groups_added == ["t1", "t2"]
    puts = [body(r) for r in transport.requests if r.method == "PUT"]
    assert puts == [
        {"primaryTeamId": "t1", "secondaryTeamIds": []},
        {"primaryTeamId": "t1", "secondaryTeamIds": ["t2"]},
    ]
    assert result.account.groups == frozenset({"t1", "t2"})


def test_removing_primary_team_promotes_secondary(mock_transport):
    state = {"user": hs_user(primary="t1", secondary=["t2", "t3"])}

    def handler(request):
        if request.method == "GET":
            return httpx.Response(200, json=state["user"])
        payload = body(request)
        state["user"] = hs_user(
            primary=payload["primaryTeamId"], secondary=payload["secondaryTeamIds"]
        )
        return httpx.Response(200, json=state["user"])

    transport = mock_transport(handler)
    result = adapter(transport).ensure_account(
        spec(), external_id="11", groups={"t2", "t3"}, managed_groups={"t1", "t2", "t3"}
    )
    assert result.groups_removed == ["t1"]
    assert body([r for r in transport.requests if r.method == "PUT"][-1]) == {
        "primaryTeamId": "t2",
        "secondaryTeamIds": ["t3"],
    }


def test_create_conflict_uses_existing_user(mock_transport):
    lookups = {"count": 0}

    def handler(request):
        if request.method == "GET" and "ada%40" in raw_path(request):
            lookups["count"] += 1
            if lookups["count"] == 1:
                return httpx.Response(404)
            return httpx.Response(200, json=hs_user())
        if request.method == "GET":
            return httpx.Response(200, json={"results": []})
        if request.method == "POST":
            return httpx.Response(409)
        raise AssertionError(request)

    result = adapter(mock_transport(handler)).ensure_account(
        spec(), groups=set(), managed_groups=set()
    )
    assert not result.created
    assert result.account.external_id == "11"


def test_disable_removes_user(mock_transport):
    def handler(request):
        if request.method == "GET":
            return httpx.Response(200, json=hs_user(primary="t1"))
        assert request.method == "DELETE"
        assert request.url.path == f"{USERS_PATH}/11"
        return httpx.Response(204)

    transport = mock_transport(handler)
    result = adapter(transport).disable_account(spec(), external_id="11", managed_groups={"t1"})
    assert result.changed
    assert result.account.active is False
    assert not any(r.method == "PUT" for r in transport.requests)


def test_deactivated_user_cannot_be_reactivated_by_api(mock_transport):
    transport = mock_transport(lambda request: httpx.Response(200, json=hs_user(deactivated=True)))
    with pytest.raises(AdapterError, match="Reactivate"):
        adapter(transport).ensure_account(
            spec(), external_id="11", groups=set(), managed_groups=set()
        )


def test_server_error_is_transient(mock_transport):
    transport = mock_transport(lambda request: httpx.Response(502))
    with pytest.raises(TransientError):
        adapter(transport).find_account(spec(), "11")


def test_list_accounts_and_teams(mock_transport):
    def handler(request):
        if request.url.path == f"{USERS_PATH}/teams":
            return httpx.Response(200, json={"results": [{"id": 7, "name": "Sales"}]})
        if request.url.params.get("after") == "c2":
            return httpx.Response(200, json={"results": [hs_user("12")]})
        return httpx.Response(
            200, json={"results": [hs_user("11")], "paging": {"next": {"after": "c2"}}}
        )

    hubspot = adapter(mock_transport(handler))
    assert [a.external_id for a in hubspot.list_accounts()] == ["11", "12"]
    groups = hubspot.list_groups()
    assert [(g.external_id, g.name) for g in groups] == [("7", "Sales")]


def test_a_listing_that_is_not_json_is_an_error(mock_transport):
    def handler(request):
        return httpx.Response(200, text="<html>Maintenance</html>")

    with pytest.raises(AdapterError, match="unreadable"):
        list(adapter(mock_transport(handler)).list_accounts())
    with pytest.raises(AdapterError, match="unreadable"):
        adapter(mock_transport(handler)).list_groups()

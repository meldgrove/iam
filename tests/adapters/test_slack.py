import httpx
import pytest

from iam.adapters.base import (
    AccountSpec,
    AdapterError,
    ConfigurationError,
    TransientError,
)
from iam.adapters.slack import SlackAdapter

TOKEN = "xoxb-test-token-value"


def spec(**kwargs):
    values = {
        "first_name": "Ada",
        "last_name": "Lovelace",
        "username": "ada",
        "email": "ada@example.invalid",
    }
    values.update(kwargs)
    return AccountSpec(**values)


def ok(**data):
    return httpx.Response(200, json={"ok": True, **data})


def error(code):
    return httpx.Response(200, json={"ok": False, "error": code})


def adapter(transport, **config):
    return SlackAdapter({"bot_token": TOKEN, **config}, transport=transport)


def test_test_connection_reports_workspace(mock_transport):
    transport = mock_transport(lambda request: ok(team="Example", team_id="T1"))
    result = adapter(transport).test_connection()
    assert result.ok
    assert "Example" in result.message
    request = transport.requests[0]
    assert request.url.path == "/api/auth.test"
    assert request.headers["Authorization"] == f"Bearer {TOKEN}"
    assert TOKEN not in str(request.url)


def test_without_token_everything_is_manual():
    slack = SlackAdapter({})
    assert not slack.can_read_accounts()
    result = slack.test_connection()
    assert result.ok and "No token" in result.message
    with pytest.raises(ConfigurationError, match="needs a bot token"):
        slack.find_account(spec())
    with pytest.raises(ConfigurationError):
        list(slack.list_accounts())
    with pytest.raises(ConfigurationError):
        slack.list_groups()


def test_invalid_auth_is_configuration_error_without_token(mock_transport):
    transport = mock_transport(lambda request: error("invalid_auth"))
    with pytest.raises(ConfigurationError) as caught:
        adapter(transport).test_connection()
    assert "invalid_auth" in str(caught.value)
    assert TOKEN not in str(caught.value)


def test_ratelimited_is_transient(mock_transport):
    transport = mock_transport(lambda request: error("ratelimited"))
    with pytest.raises(TransientError):
        adapter(transport).find_account(spec())


def test_http_429_is_transient(mock_transport):
    transport = mock_transport(lambda request: httpx.Response(429))
    with pytest.raises(TransientError):
        adapter(transport).test_connection()


def test_lookup_by_email_found(mock_transport):
    def handler(request):
        assert request.url.path == "/api/users.lookupByEmail"
        assert request.url.params["email"] == "ada@example.invalid"
        return ok(
            user={
                "id": "U1",
                "name": "ada",
                "deleted": False,
                "profile": {"email": "ada@example.invalid", "real_name": "Ada Lovelace"},
            }
        )

    account = adapter(mock_transport(handler)).find_account(spec())
    assert account.external_id == "U1"
    assert account.email == "ada@example.invalid"
    assert account.display_name == "Ada Lovelace"
    assert account.active
    assert account.groups is None


def test_lookup_by_email_not_found(mock_transport):
    transport = mock_transport(lambda request: error("users_not_found"))
    assert adapter(transport).find_account(spec()) is None


def test_find_by_external_id_first_then_email(mock_transport):
    def handler(request):
        if request.url.path == "/api/users.info":
            return error("user_not_found")
        return ok(user={"id": "U2", "deleted": True, "profile": {"email": "ada@example.invalid"}})

    transport = mock_transport(handler)
    account = adapter(transport).find_account(spec(), external_id="U9")
    assert [r.url.path for r in transport.requests] == [
        "/api/users.info",
        "/api/users.lookupByEmail",
    ]
    assert account.external_id == "U2"
    assert not account.active


def test_find_by_external_id(mock_transport):
    transport = mock_transport(lambda request: ok(user={"id": "U9", "name": "ada", "profile": {}}))
    account = adapter(transport).find_account(spec(email=""), external_id="U9")
    assert account.external_id == "U9"
    assert len(transport.requests) == 1


def test_unknown_slack_error_is_adapter_error(mock_transport):
    transport = mock_transport(lambda request: error("fatal_error"))
    with pytest.raises(AdapterError, match="fatal_error"):
        adapter(transport).find_account(spec())


def test_list_accounts_pages_and_skips_bots(mock_transport):
    pages = {
        "": {
            "members": [
                {"id": "U1", "name": "ada", "profile": {"email": "ada@example.invalid"}},
                {"id": "B1", "name": "bot", "is_bot": True, "profile": {}},
                {"id": "USLACKBOT", "name": "slackbot", "profile": {}},
            ],
            "response_metadata": {"next_cursor": "page2"},
        },
        "page2": {
            "members": [
                {"id": "U2", "name": "old", "deleted": True, "profile": {}},
                {"id": "A1", "name": "app", "is_app_user": True, "profile": {}},
            ],
            "response_metadata": {"next_cursor": ""},
        },
    }

    def handler(request):
        assert request.url.path == "/api/users.list"
        return ok(**pages[request.url.params.get("cursor", "")])

    accounts = list(adapter(mock_transport(handler)).list_accounts())
    assert [a.external_id for a in accounts] == ["U1", "U2"]
    assert accounts[0].active and not accounts[1].active


def test_a_rate_limit_in_the_middle_of_a_listing_waits_and_goes_on(mock_transport, pauses):
    # Slack allows about 20 users.list pages a minute and answers 429 beyond that.
    limited = []

    def handler(request):
        cursor = request.url.params.get("cursor", "")
        if cursor == "page2" and not limited:
            limited.append(cursor)
            return httpx.Response(429, headers={"Retry-After": "3"})
        members = [{"id": f"U-{cursor or 'page1'}", "name": cursor or "ada", "profile": {}}]
        return ok(members=members, response_metadata={"next_cursor": "" if cursor else "page2"})

    accounts = list(adapter(mock_transport(handler)).list_accounts())
    assert [a.external_id for a in accounts] == ["U-page1", "U-page2"]
    assert pauses == [3.0]


def test_list_accounts_rejects_repeated_cursor(mock_transport):
    transport = mock_transport(
        lambda request: ok(members=[], response_metadata={"next_cursor": "same"})
    )
    with pytest.raises(AdapterError, match="repeated"):
        list(adapter(transport).list_accounts())


def test_list_groups_pages_channels(mock_transport):
    pages = {
        "": {
            "channels": [
                {"id": "C1", "name": "general", "purpose": {"value": "Everyone"}},
                {"id": "C2", "name": "old", "is_archived": True},
            ],
            "response_metadata": {"next_cursor": "next"},
        },
        "next": {"channels": [{"id": "G1", "name": "private"}], "response_metadata": {}},
    }

    def handler(request):
        assert request.url.path == "/api/conversations.list"
        assert request.url.params["types"] == "public_channel,private_channel"
        assert request.url.params["exclude_archived"] == "true"
        return ok(**pages[request.url.params.get("cursor", "")])

    groups = adapter(mock_transport(handler)).list_groups()
    assert [(g.external_id, g.name) for g in groups] == [("C1", "#general"), ("G1", "#private")]
    assert groups[0].description == "Everyone"


def test_unexpected_answer(mock_transport):
    transport = mock_transport(lambda request: httpx.Response(200, content=b"<html>"))
    with pytest.raises(AdapterError, match="unexpected"):
        adapter(transport).test_connection()


def test_manual_instructions_name_channels_and_role():
    slack = SlackAdapter({"workspace_url": "https://example.slack.com"})
    text = slack.manual_instructions("ensure_account", spec(), ["#general", "#team"])
    assert "#general, #team" in text
    assert "full member" in text
    assert "https://example.slack.com" in text
    assert "cannot read Slack" in text

    guest = slack.manual_instructions("ensure_account", spec(person_type="external"), [])
    assert "guest" in guest

    disable = SlackAdapter({"bot_token": TOKEN}).manual_instructions("disable_account", spec(), [])
    assert "Deactivate" in disable
    assert "cannot read" not in disable


def test_capabilities():
    assert SlackAdapter.supports_write is False
    assert SlackAdapter.supports_groups is True
    assert SlackAdapter.group_label == "Channels"
    secret_fields = [f.name for f in SlackAdapter.fields if f.secret]
    assert secret_fields == ["bot_token"]


def test_requests_carry_no_token_in_body_or_url(mock_transport):
    transport = mock_transport(lambda request: ok(team="T"))
    adapter(transport).test_connection()
    request = transport.requests[0]
    assert TOKEN not in str(request.url)
    assert TOKEN not in (request.content or b"").decode()

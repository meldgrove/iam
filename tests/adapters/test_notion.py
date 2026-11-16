import httpx
import pytest

from iam.adapters.base import AccountSpec, AdapterError, ConfigurationError, TransientError
from iam.adapters.notion import NOTION_VERSION, NotionAdapter

TOKEN = "ntn_test_token_value"


def spec(**kwargs):
    values = {
        "first_name": "Ada",
        "last_name": "Lovelace",
        "username": "ada",
        "email": "Ada@example.invalid",
    }
    values.update(kwargs)
    return AccountSpec(**values)


def person(user_id, email, name="Someone"):
    return {
        "object": "user",
        "id": user_id,
        "type": "person",
        "name": name,
        "person": {"email": email},
    }


def bot(user_id):
    return {"object": "user", "id": user_id, "type": "bot", "name": "Integration", "bot": {}}


def adapter(transport, **config):
    return NotionAdapter({"token": TOKEN, **config}, transport=transport)


def paged(pages):
    def handler(request):
        assert request.url.path == "/v1/users"
        assert request.url.params["page_size"] == "100"
        return httpx.Response(200, json=pages[request.url.params.get("start_cursor", "")])

    return handler


def test_test_connection_names_workspace(mock_transport):
    transport = mock_transport(
        lambda request: httpx.Response(
            200, json={"type": "bot", "bot": {"workspace_name": "Example"}}
        )
    )
    result = adapter(transport).test_connection()
    assert result.ok and "Example" in result.message
    request = transport.requests[0]
    assert request.url.path == "/v1/users/me"
    assert request.headers["Notion-Version"] == NOTION_VERSION
    assert request.headers["Authorization"] == f"Bearer {TOKEN}"


def test_without_token_everything_is_manual():
    notion = NotionAdapter({})
    assert not notion.can_read_accounts()
    result = notion.test_connection()
    assert result.ok and "No token" in result.message
    with pytest.raises(ConfigurationError, match="needs a token"):
        notion.find_account(spec())
    with pytest.raises(ConfigurationError):
        list(notion.list_accounts())


def test_invalid_token_is_configuration_error(mock_transport):
    transport = mock_transport(
        lambda request: httpx.Response(401, json={"message": "API token is invalid."})
    )
    with pytest.raises(ConfigurationError) as caught:
        adapter(transport).test_connection()
    assert TOKEN not in str(caught.value)


def test_rate_limit_is_transient(mock_transport):
    transport = mock_transport(lambda request: httpx.Response(429))
    with pytest.raises(TransientError):
        list(adapter(transport).list_accounts())


def test_list_accounts_pages_and_skips_bots(mock_transport):
    pages = {
        "": {
            "results": [person("u1", "ada@example.invalid"), bot("b1")],
            "has_more": True,
            "next_cursor": "c2",
        },
        "c2": {"results": [person("u2", "")], "has_more": False, "next_cursor": None},
    }
    accounts = list(adapter(mock_transport(paged(pages))).list_accounts())
    assert [a.external_id for a in accounts] == ["u1", "u2"]
    assert accounts[0].email == "ada@example.invalid"
    assert all(a.active for a in accounts)


def test_paging_without_cursor_is_error(mock_transport):
    pages = {"": {"results": [], "has_more": True, "next_cursor": None}}
    with pytest.raises(AdapterError, match="inconsistent"):
        list(adapter(mock_transport(paged(pages))).list_accounts())


def test_find_account_matches_email_case_insensitively(mock_transport):
    pages = {
        "": {
            "results": [bot("b1"), person("u1", "other@example.invalid")],
            "has_more": True,
            "next_cursor": "c2",
        },
        "c2": {"results": [person("u2", "ada@example.invalid", "Ada")], "has_more": False},
    }
    account = adapter(mock_transport(paged(pages))).find_account(spec())
    assert account.external_id == "u2"
    assert account.display_name == "Ada"


def test_find_account_not_found(mock_transport):
    pages = {"": {"results": [person("u1", "other@example.invalid")], "has_more": False}}
    assert adapter(mock_transport(paged(pages))).find_account(spec()) is None


def test_find_account_with_hidden_emails_is_uncertain(mock_transport):
    pages = {"": {"results": [person("u1", "")], "has_more": False}}
    with pytest.raises(AdapterError, match="hides the email"):
        adapter(mock_transport(paged(pages))).find_account(spec())


def test_find_account_by_external_id(mock_transport):
    def handler(request):
        assert request.url.path == "/v1/users/u7"
        return httpx.Response(200, json=person("u7", "ada@example.invalid"))

    transport = mock_transport(handler)
    account = adapter(transport).find_account(spec(), external_id="u7")
    assert account.external_id == "u7"
    assert len(transport.requests) == 1


def test_unknown_external_id_falls_back_to_email(mock_transport):
    def handler(request):
        if request.url.path == "/v1/users/gone":
            return httpx.Response(404, json={"object": "error"})
        return httpx.Response(
            200, json={"results": [person("u3", "ada@example.invalid")], "has_more": False}
        )

    account = adapter(mock_transport(handler)).find_account(spec(), external_id="gone")
    assert account.external_id == "u3"


def test_server_error_on_user_lookup_is_transient(mock_transport):
    transport = mock_transport(lambda request: httpx.Response(503))
    with pytest.raises(TransientError):
        adapter(transport).find_account(spec(), external_id="u1")


def test_manual_instructions():
    notion = NotionAdapter({"workspace_url": "https://www.notion.so/example"})
    invite = notion.manual_instructions("ensure_account", spec(), [])
    assert "Invite" in invite and "Ada@example.invalid" in invite
    assert "https://www.notion.so/example" in invite
    assert "cannot read Notion" in invite
    remove = NotionAdapter({"token": TOKEN}).manual_instructions("disable_account", spec(), [])
    assert "Remove" in remove
    assert "cannot read" not in remove


def test_capabilities():
    assert NotionAdapter.supports_write is False
    assert NotionAdapter.supports_groups is False
    assert [f.name for f in NotionAdapter.fields if f.secret] == ["token"]

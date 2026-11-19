"""In-app updates: the admin routes in front of the Meldgrove Updater, the
update button's place in the page, and the settings section (docs/updates.md)."""

import copy
import json

import httpx
import pytest
from django.test import Client
from django.urls import reverse

from iam import updates
from iam.models import AuditEvent

pytestmark = pytest.mark.django_db

STATUS = {
    "updater": "0.2.0",
    "mode": "host",
    "channel": "stable",
    "channelLocked": None,
    "installed": "0.2.0",
    "available": {
        "version": "0.2.1",
        "preview": False,
        "publishedAt": "2026-11-17T10:00:00Z",
        "url": "https://github.com/meldgrove/iam/releases/tag/v0.2.1",
        "notes": ["Fix the sign-in loop behind some proxies"],
        "changes": 1,
        "installsAt": "2026-11-20T02:00:00Z",
        "failed": None,
    },
    "attention": None,
    "installing": None,
    "last": None,
    "checking": False,
    "checkedAt": "2026-11-19T08:00:00Z",
    "error": None,
    "automatic": True,
    "cooldown": "3d",
    "window": "02:00-05:00",
    "timezone": "Europe/Berlin",
}
ROUTES = [
    ("get", "updates", None),
    ("post", "updates_check", {}),
    ("post", "updates_install", {"version": "0.2.1"}),
    ("post", "updates_channel", {"channel": "preview"}),
]


def answer(routes):
    """A handler: {"POST /v1/install": (409, {...})}; GET /v1/status answers STATUS."""

    def handle(request):
        code, body = routes.get(f"{request.method} {request.url.path}", (200, STATUS))
        if isinstance(body, str):
            return httpx.Response(code, text=body)
        return httpx.Response(code, json=body)

    return handle


def call(client, method, name, payload=None):
    if method == "get":
        return client.get(reverse(name))
    return client.post(reverse(name), json.dumps(payload or {}), content_type="application/json")


@pytest.fixture
def admin(client_for, admin_user):
    return client_for(admin_user)


@pytest.mark.parametrize("role", ["viewer", "approver"])
def test_only_admins_reach_the_updater(client_for, role, updater):
    from tests.conftest import make_user

    updater.handler = answer({})
    client = client_for(make_user(role))
    for method, name, payload in ROUTES:
        assert call(client, method, name, payload).status_code == 403, name
    assert updater.requests == []


def test_signed_out_requests_go_to_the_sign_in(client, updater):
    updater.handler = answer({})
    for method, name, payload in ROUTES:
        response = call(client, method, name, payload)
        assert response.status_code == 302 and "/login/" in response["Location"], name
    assert updater.requests == []


def test_the_status_passes_through(admin, updater, settings):
    settings.APP_VERSION = "0.2.0"
    updater.handler = answer({})
    response = admin.get(reverse("updates"))
    assert response.status_code == 200
    assert response.json() == {
        "app": {"version": "0.2.0"},
        "reachable": True,
        "updater": STATUS,
        "error": None,
    }
    assert [(r.method, r.url.path) for r in updater.requests] == [("GET", "/v1/status")]


def test_development_has_no_app_version(admin, updater, settings):
    settings.APP_VERSION = ""
    updater.handler = answer({})
    assert admin.get(reverse("updates")).json()["app"] == {"version": None}


def test_each_call_has_its_timeout(admin, updater):
    updater.handler = answer({"POST /v1/install": (202, STATUS)})
    for method, name, payload in ROUTES:
        call(admin, method, name, payload)
    timeouts = {r.url.path: r.extensions["timeout"]["read"] for r in updater.requests}
    assert timeouts == {
        "/v1/status": 3,
        "/v1/check": 35,
        "/v1/install": 10,
        "/v1/channel": 35,
    }
    assert {r.extensions["timeout"]["connect"] for r in updater.requests} == {3}


def test_an_unreachable_updater_answers_200(admin, updater):
    # The fake refuses connections until told otherwise.
    for method, name, payload in ROUTES:
        response = call(admin, method, name, payload)
        assert response.status_code == 200, name
        body = response.json()
        assert body["reachable"] is False and body["updater"] is None, name
        assert body["error"] == "No connection to http://updater:8080."


def test_a_timeout_counts_as_unreachable(admin, updater):
    def slow(request):
        raise httpx.ReadTimeout("timed out", request=request)

    updater.handler = slow
    body = admin.post(reverse("updates_check"), "{}", content_type="application/json").json()
    assert body["reachable"] is False
    assert body["error"] == "The update service did not answer within 35 s."


def test_an_answer_that_is_no_status_counts_as_unreachable(admin, updater):
    updater.handler = answer({"GET /v1/status": (200, "<html>a proxy page</html>")})
    body = admin.get(reverse("updates")).json()
    assert body["reachable"] is False and body["updater"] is None


def test_an_empty_updater_url_switches_updates_off(admin, updater, settings):
    settings.UPDATER_URL = ""
    response = admin.get(reverse("updates"))
    assert response.status_code == 200 and response.json()["reachable"] is False
    assert updater.requests == []
    assert b"data-updates" not in admin.get(reverse("dashboard")).content


@pytest.mark.parametrize(
    "version",
    [
        "",
        "latest",
        "0.2",
        "v0.2.1",
        "0.2.1-rc.1",
        "0.2.1\n",
        "sha256:0123",
        "0.2.1 && true",
        5,
        None,
    ],
)
def test_install_takes_only_versions(admin, updater, version):
    updater.handler = answer({})
    response = admin.post(
        reverse("updates_install"),
        json.dumps({"version": version}),
        content_type="application/json",
    )
    assert response.status_code == 400
    assert response.json()["error"] == "Name a version such as 0.2.1."
    assert response.json()["updater"] == STATUS
    assert all(r.url.path != "/v1/install" for r in updater.requests)


@pytest.mark.parametrize("version", ["0.2.1", "10.20.30-preview.3", "sha256:0123456789ab"])
def test_install_passes_the_version_and_audits(admin, admin_user, updater, version):
    installing = copy.deepcopy(STATUS) | {
        "installing": {
            "version": version,
            "startedAt": "2026-11-19T09:00:00Z",
            "manual": True,
            "stage": "requested",
        }
    }
    updater.handler = answer({"POST /v1/install": (202, installing)})
    response = admin.post(
        reverse("updates_install"),
        json.dumps({"version": version}),
        content_type="application/json",
    )
    assert response.status_code == 202
    assert response.json()["updater"]["installing"]["version"] == version
    sent = [r for r in updater.requests if r.url.path == "/v1/install"]
    assert [json.loads(r.content) for r in sent] == [{"version": version}]
    event = AuditEvent.objects.get(action="updates.install")
    assert event.actor == admin_user and event.details == {"version": version}


def test_a_refusal_comes_back_as_409_with_the_latest_status(admin, updater):
    updater.handler = answer({"POST /v1/install": (409, {"error": "0.2.1 is being installed."})})
    response = admin.post(
        reverse("updates_install"),
        json.dumps({"version": "0.2.1"}),
        content_type="application/json",
    )
    assert response.status_code == 409
    assert response.json() == {
        "app": {"version": None},
        "reachable": True,
        "updater": STATUS,
        "error": "0.2.1 is being installed.",
    }
    assert not AuditEvent.objects.filter(action="updates.install").exists()


def test_an_updater_error_without_a_sentence_still_explains(admin, updater):
    updater.handler = answer({"POST /v1/check": (500, "boom")})
    response = admin.post(reverse("updates_check"), "{}", content_type="application/json")
    assert response.status_code == 500
    assert response.json()["error"] == "The update service answered HTTP 500."


def test_an_updater_failure_on_install_stays_a_5xx(admin, updater):
    # The updater may have taken the request: the button must not read it as
    # a refusal (409) and forget the installation.
    updater.handler = answer({"POST /v1/install": (503, {"error": "The volume is not writable."})})
    response = admin.post(
        reverse("updates_install"),
        json.dumps({"version": "0.2.1"}),
        content_type="application/json",
    )
    assert response.status_code == 503
    assert response.json() == {
        "app": {"version": None},
        "reachable": True,
        "updater": STATUS,
        "error": "The volume is not writable.",
    }
    assert not AuditEvent.objects.filter(action="updates.install").exists()


def test_channel_takes_only_stable_or_preview(admin, updater):
    updater.handler = answer({"POST /v1/channel": (200, STATUS)})
    for channel in ["", "beta", "Preview", None, ["preview"]]:
        response = admin.post(
            reverse("updates_channel"),
            json.dumps({"channel": channel}),
            content_type="application/json",
        )
        assert response.status_code == 400, channel
        assert response.json()["error"] == "The channel is stable or preview."
    assert all(r.url.path != "/v1/channel" for r in updater.requests)
    response = admin.post(reverse("updates_install"), "not json", content_type="application/json")
    assert response.status_code == 400


def test_switching_the_channel_from_the_button(admin, admin_user, updater):
    preview = STATUS | {"channel": "preview", "cooldown": None}
    updater.handler = answer({"POST /v1/channel": (200, preview)})
    response = admin.post(
        reverse("updates_channel"),
        json.dumps({"channel": "preview"}),
        content_type="application/json",
    )
    assert response.status_code == 200 and response.json()["updater"]["channel"] == "preview"
    sent = [json.loads(r.content) for r in updater.requests if r.url.path == "/v1/channel"]
    assert sent == [{"channel": "preview"}]
    assert AuditEvent.objects.get(action="updates.channel").details == {"channel": "preview"}


def test_switching_the_channel_from_the_settings_form(admin, updater):
    updater.handler = answer({"POST /v1/channel": (200, STATUS | {"channel": "preview"})})
    response = admin.post(reverse("updates_channel"), {"channel": "preview"})
    assert response.status_code == 302
    assert response["Location"] == reverse("settings") + "#updates"
    page = admin.get(response["Location"]).content.decode()
    assert "Switched the update channel to Preview." in page

    locked = "The platform's image tag latest decides which updates install. Change it there."
    updater.handler = answer({"POST /v1/channel": (409, {"error": locked})})
    page = admin.post(reverse("updates_channel"), {"channel": "stable"}, follow=True)
    assert locked.replace("'", "&#x27;") in page.content.decode()
    assert AuditEvent.objects.filter(action="updates.channel").count() == 1


def test_a_channel_counts_as_saved_only_when_the_status_names_it(admin, updater):
    # The updater answered, but its status still names the old channel.
    updater.handler = answer({"POST /v1/channel": (200, STATUS)})
    response = admin.post(
        reverse("updates_channel"),
        json.dumps({"channel": "preview"}),
        content_type="application/json",
    )
    assert response.status_code == 409
    assert response.json()["error"] == "The update service did not switch the channel."
    assert response.json()["updater"]["channel"] == "stable"
    page = admin.post(reverse("updates_channel"), {"channel": "preview"}, follow=True)
    content = page.content.decode()
    assert "The update service did not switch the channel." in content
    assert "Switched the update channel" not in content
    assert not AuditEvent.objects.filter(action="updates.channel").exists()


def test_posts_need_the_csrf_token(admin_user, updater):
    updater.handler = answer({})
    client = Client(enforce_csrf_checks=True)
    client.force_login(admin_user, backend="iam.auth.OIDCBackend")
    url = reverse("updates_check")
    assert client.post(url, "{}", content_type="application/json").status_code == 403
    page = client.get(reverse("dashboard")).content.decode()
    token = page.split('data-csrf="', 1)[1].split('"', 1)[0]
    response = client.post(
        url, "{}", content_type="application/json", headers={"X-CSRFToken": token}
    )
    assert response.status_code == 200


def test_the_button_is_for_admins_only(client_for, admin_user, viewer_user, updater):
    page = client_for(admin_user).get(reverse("dashboard")).content.decode()
    assert "data-updates" in page and "iam/updates.js" in page
    assert updater.requests == [], "The page itself never waits for the updater."
    page = client_for(viewer_user).get(reverse("dashboard")).content.decode()
    assert "data-updates" not in page and "updates.js" not in page


def test_the_settings_section(admin, updater):
    updater.handler = answer({})
    page = admin.get(reverse("settings")).content.decode()
    assert 'id="updates"' in page
    assert "On: releases install automatically after 3 days, between 02:00 and 05:00" in page
    assert "(Europe/Berlin)." in page
    assert 'value="stable" checked' in page
    assert "install automatically after 3 days." in page
    assert "Switching back to Stable keeps this version until a release is newer." in page
    assert "Save channel" in page and "disabled" not in page.split('id="updates"', 1)[1]


def test_the_settings_section_with_a_locked_channel(admin, updater):
    reason = "The platform's image tag latest decides which updates install. Change it there."
    updater.handler = answer(
        {"GET /v1/status": (200, STATUS | {"mode": "platform", "channelLocked": reason})}
    )
    section = admin.get(reverse("settings")).content.decode().split('id="updates"', 1)[1]
    assert '<fieldset class="choices" disabled>' in section
    assert reason.replace("'", "&#x27;") in section
    assert "The platform installs them (deploy link)." in section
    assert "Save channel" not in section


def test_the_settings_section_without_an_updater(admin, updater, settings):
    settings.APP_VERSION = ""
    section = admin.get(reverse("settings")).content.decode().split('id="updates"', 1)[1]
    assert "The update service is not reachable." in section
    assert "<dd>development</dd>" in section
    assert '<fieldset class="choices" disabled>' in section


@pytest.mark.parametrize(
    ("value", "text"),
    [
        ("3d", "3 days"),
        ("1d", "1 day"),
        ("12h", "12 hours"),
        ("30m", "30 minutes"),
        ("90", "90 seconds"),
        ("0", ""),
        (None, ""),
    ],
)
def test_describe_duration(value, text):
    assert updates.describe_duration(value) == text


def test_automatic_installs():
    assert updates.automatic_installs(STATUS | {"window": None}) == (
        "On: releases install automatically after 3 days."
    )
    assert updates.automatic_installs(STATUS | {"channel": "preview", "cooldown": None}) == (
        "On: previews and releases install automatically, between 02:00 and 05:00 (Europe/Berlin)."
    )
    assert updates.automatic_installs(STATUS | {"mode": "off", "automatic": False}) == (
        "Off on this server: nothing installs automatically."
    )

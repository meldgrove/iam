import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def load_settings(**variables: str) -> dict:
    """Import config.settings in a fresh process with only these variables."""
    code = (
        "import json, config.settings as s; print(json.dumps({name: getattr(s, name) for name in"
        " ['SENTRY_RELEASE', 'SENTRY_ENVIRONMENT', 'OIDC_PROVIDER_NAME', 'SESSION_COOKIE_AGE',"
        " 'SECURE_SSL_REDIRECT', 'OIDC_RP_SCOPES']}))"
    )
    env = {"PATH": os.environ.get("PATH", ""), **variables}
    result = subprocess.run(
        [sys.executable, "-c", code], cwd=ROOT, env=env, capture_output=True, text=True, check=True
    )
    return json.loads(result.stdout)


PRODUCTION = {
    "DJANGO_SECRET_KEY": "x" * 50,
    "ENCRYPTION_KEY": "Ofk3gL0a5Q4bH3rYp9qV0wXz1nM2cT5uE6sD7fG8hJ0=",
    "ALLOWED_HOSTS": "iam.example.invalid",
    "APP_VERSION": "1.2.3",
    "KEYCLOAK_URL": "https://sso.example.invalid",
    "KEYCLOAK_REALM": "staff",
}


def test_empty_variables_from_compose_keep_the_defaults():
    # compose.yaml passes every optional variable, unset ones as "".
    settings = load_settings(
        **PRODUCTION,
        SENTRY_RELEASE="",
        SENTRY_ENVIRONMENT="",
        OIDC_PROVIDER_NAME="",
        SESSION_HOURS="",
        SECURE_SSL_REDIRECT="",
        OIDC_SCOPES="",
    )
    assert settings == {
        "SENTRY_RELEASE": "iam@1.2.3",
        "SENTRY_ENVIRONMENT": "production",
        "OIDC_PROVIDER_NAME": "Keycloak",
        "SESSION_COOKIE_AGE": 12 * 3600,
        "SECURE_SSL_REDIRECT": True,
        "OIDC_RP_SCOPES": "openid email profile",
    }


def test_set_variables_win_over_the_defaults():
    settings = load_settings(
        **PRODUCTION,
        SENTRY_RELEASE="custom",
        SENTRY_ENVIRONMENT="staging-eu",
        OIDC_PROVIDER_NAME="Staff login",
    )
    assert settings["SENTRY_RELEASE"] == "custom"
    assert settings["SENTRY_ENVIRONMENT"] == "staging-eu"
    assert settings["OIDC_PROVIDER_NAME"] == "Staff login"


def test_sentry_omits_sensitive_request_and_provider_metadata():
    code = """
import io
import json
import logging
from wsgiref.util import setup_testing_defaults

import django
import sentry_sdk
from django.test import RequestFactory
from sentry_sdk.integrations.wsgi import SentryWsgiMiddleware
from sentry_sdk.transport import Transport

django.setup()
events = {}

class Sink(Transport):
    def capture_envelope(self, envelope):
        for item in envelope.items:
            if item.type in {"event", "transaction"}:
                events.setdefault(item.type, []).append(item.payload.json)

client = sentry_sdk.get_client()
client.transport = Sink(client.options)
request = RequestFactory().get("/oidc/callback/?code=sample-code")

def app(environ, start_response):
    logging.getLogger("django.security.SuspiciousOperation").error(
        "Invalid state", extra={"request": request}
    )
    logging.getLogger("django.security.csrf").warning("Rejected referrer: sample-referrer")
    logging.getLogger("django.request").error("Request failed", extra={"request": request})
    sentry_sdk.capture_message("Synthetic error event")
    start_response("200 OK", [])
    return [b""]

environ = {
    "wsgi.input": io.BytesIO(), "wsgi.url_scheme": "https", "SERVER_NAME": "iam.example.invalid",
    "SERVER_PORT": "443", "PATH_INFO": "/oidc/callback/", "REQUEST_METHOD": "GET",
    "QUERY_STRING": "code=sample-code", "HTTP_X_HEALTH_TOKEN": "sample-header",
}
setup_testing_defaults(environ)
response = SentryWsgiMiddleware(app)(environ, lambda status, headers: None)
list(response)
response.close()

print(json.dumps({
    "events": events,
    "privacy_options": [client.options[key] for key in (
        "send_default_pii", "include_local_variables", "max_request_body_size"
    )],
    "integrations": list(client.integrations),
}))
"""
    env = {
        "PATH": os.environ.get("PATH", ""),
        **PRODUCTION,
        "DJANGO_SETTINGS_MODULE": "config.settings",
        "SENTRY_DSN": "https://public@sentry.example.invalid/1",
        "SENTRY_TRACES_SAMPLE_RATE": "1",
    }
    result = subprocess.run(
        [sys.executable, "-c", code], cwd=ROOT, env=env, capture_output=True, text=True, check=True
    )
    data = json.loads(result.stdout)
    assert set(data["events"]) == {"event", "transaction"}
    for events in data["events"].values():
        (event,) = events
        assert event["request"] == {
            "url": "https://iam.example.invalid/oidc/callback/",
            "method": "GET",
        }
    assert "sample-" not in result.stdout
    assert data["privacy_options"] == [False, False, "never"]
    assert not {"httpx", "stdlib"} & set(data["integrations"])


def load_database(url: str) -> dict:
    code = "import json, config.settings as s; print(json.dumps(s.DATABASES['default']))"
    env = {"PATH": os.environ.get("PATH", ""), **PRODUCTION, "DATABASE_URL": url}
    result = subprocess.run(
        [sys.executable, "-c", code], cwd=ROOT, env=env, capture_output=True, text=True, check=True
    )
    return json.loads(result.stdout)


def test_database_connections_survive_restarts_and_failovers():
    database = load_database("postgresql://iam:secret@db.example.invalid/iam?connect_timeout=3")
    assert database["CONN_HEALTH_CHECKS"] is True
    # A value in the URL wins; the others bound how long a dead server can hold a request.
    assert database["OPTIONS"] == {
        "connect_timeout": 3,
        "keepalives": 1,
        "keepalives_idle": 30,
        "keepalives_interval": 10,
        "keepalives_count": 3,
    }
    # SQLite knows none of these options.
    assert "OPTIONS" not in load_database("sqlite:////tmp/iam-settings-test.sqlite3")


def load_updater_url(**variables: str) -> subprocess.CompletedProcess:
    code = "import config.settings as s; print(repr(s.UPDATER_URL))"
    env = {"PATH": os.environ.get("PATH", ""), **PRODUCTION, **variables}
    return subprocess.run(
        [sys.executable, "-c", code], cwd=ROOT, env=env, capture_output=True, text=True
    )


def test_updater_url_defaults_to_the_compose_service_and_empty_switches_it_off():
    assert load_updater_url().stdout.strip() == "'http://updater:8080'"
    assert load_updater_url(UPDATER_URL="").stdout.strip() == "''"
    assert load_updater_url(UPDATER_URL="http://127.0.0.1:8080/").stdout.strip() == (
        "'http://127.0.0.1:8080'"
    )
    refused = load_updater_url(UPDATER_URL="updater:8080")
    assert refused.returncode != 0 and "UPDATER_URL must be" in refused.stderr

import time
from urllib.parse import parse_qs, urlsplit

import pytest
from django.core.cache import cache
from django.core.exceptions import SuspiciousOperation
from django.http.response import MAX_URL_REDIRECT_LENGTH
from django.test import RequestFactory

from iam import auth
from iam.models import Role, User

pytestmark = pytest.mark.django_db

ISSUER = "https://sso.example.invalid/realms/test"
DISCOVERY = {
    "issuer": ISSUER,
    "authorization_endpoint": f"{ISSUER}/protocol/openid-connect/auth",
    "token_endpoint": f"{ISSUER}/protocol/openid-connect/token",
    "userinfo_endpoint": f"{ISSUER}/protocol/openid-connect/userinfo",
    "jwks_uri": f"{ISSUER}/protocol/openid-connect/certs",
    "end_session_endpoint": f"{ISSUER}/protocol/openid-connect/logout",
}


class FakeResponse:
    def __init__(self, data, status=200):
        self.data, self.status_code = data, status

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests

            raise requests.HTTPError(str(self.status_code))

    def json(self):
        return self.data


@pytest.fixture(autouse=True)
def discovery_document(monkeypatch):
    cache.clear()
    calls = []

    def fake_get(url, timeout):
        calls.append(url)
        return FakeResponse(DISCOVERY)

    monkeypatch.setattr(auth.requests, "get", fake_get)
    yield calls
    cache.clear()


def claims(**extra):
    return {"sub": "subject-1", "email": "ada@example.invalid", "email_verified": True, **extra}


def test_discovery_is_fetched_once_and_cached(discovery_document):
    assert auth.discovery()["token_endpoint"].endswith("/token")
    auth.discovery()
    assert discovery_document == [f"{ISSUER}/.well-known/openid-configuration"]


def test_discovery_rejects_another_issuer(monkeypatch):
    monkeypatch.setattr(
        auth.requests,
        "get",
        lambda url, timeout: FakeResponse({**DISCOVERY, "issuer": "https://evil.example.invalid"}),
    )
    with pytest.raises(auth.ProviderUnavailable):
        auth.discovery()


@pytest.mark.parametrize(
    "invalid_document",
    [
        [],
        {**DISCOVERY, "issuer": None},
        {**DISCOVERY, "token_endpoint": None},
        {**DISCOVERY, "userinfo_endpoint": "/userinfo"},
        {**DISCOVERY, "authorization_endpoint": f"{ISSUER}/\ud800"},
        {**DISCOVERY, "token_endpoint": "https://-sso.example.invalid/token"},
        {**DISCOVERY, "token_endpoint": "https://☕.example.invalid/token"},
        {**DISCOVERY, "authorization_endpoint": f"{ISSUER}/" + "a" * MAX_URL_REDIRECT_LENGTH},
        {**DISCOVERY, "jwks_uri": "https://sso.example.invalid:invalid/keys"},
        {key: value for key, value in DISCOVERY.items() if key != "jwks_uri"},
    ],
)
def test_invalid_discovery_returns_503_and_is_retried(client, monkeypatch, invalid_document):
    answers = iter([invalid_document, DISCOVERY])
    monkeypatch.setattr(auth.requests, "get", lambda url, timeout: FakeResponse(next(answers)))

    response = client.get("/oidc/authenticate/")
    assert response.status_code == 503
    assert b"cannot be reached" in response.content
    response = client.get("/oidc/authenticate/")
    assert response.status_code == 302
    assert response["Location"].startswith(DISCOVERY["authorization_endpoint"])


def test_missing_endpoint_can_be_supplied_by_an_override(settings, monkeypatch):
    document = {**DISCOVERY, "token_endpoint": None}
    monkeypatch.setattr(auth.requests, "get", lambda url, timeout: FakeResponse(document))
    settings.OIDC_OP_TOKEN_ENDPOINT = f"{ISSUER}/override"
    assert auth.discovery()["token_endpoint"] == settings.OIDC_OP_TOKEN_ENDPOINT


def test_symmetric_signing_does_not_require_jwks(settings, monkeypatch):
    settings.OIDC_RP_SIGN_ALGO = "HS256"
    document = {key: value for key, value in DISCOVERY.items() if key != "jwks_uri"}
    monkeypatch.setattr(auth.requests, "get", lambda url, timeout: FakeResponse(document))
    assert auth.discovery()["authorization_endpoint"] == DISCOVERY["authorization_endpoint"]


def test_sign_in_redirect_over_the_length_limit_returns_503(client, monkeypatch):
    endpoint = f"{ISSUER}/".ljust(MAX_URL_REDIRECT_LENGTH, "a")
    document = {**DISCOVERY, "authorization_endpoint": endpoint}
    monkeypatch.setattr(auth.requests, "get", lambda url, timeout: FakeResponse(document))
    assert client.get("/oidc/authenticate/").status_code == 503


@pytest.mark.parametrize(
    "bad_logout", ["/logout", f"{ISSUER}/\ud800", f"{ISSUER}/".ljust(MAX_URL_REDIRECT_LENGTH, "a")]
)
def test_invalid_optional_logout_endpoint_leaves_sign_in_available_and_logout_local(
    client, monkeypatch, settings, bad_logout
):
    document = {**DISCOVERY, "end_session_endpoint": bad_logout}
    monkeypatch.setattr(auth.requests, "get", lambda url, timeout: FakeResponse(document))

    assert client.get("/oidc/authenticate/").status_code == 302
    settings.ALLOWED_HOSTS = ["iam.example.invalid"]
    request = RequestFactory().post("/logout/", HTTP_HOST="iam.example.invalid")
    request.session = {"oidc_id_token": "id-token"}
    assert auth.provider_logout_url(request) == "http://iam.example.invalid/login/"


def test_existing_user_matches_by_subject_then_verified_email():
    backend = auth.OIDCBackend()
    invited = User.objects.create_user(email="ada@example.invalid", role=Role.APPROVER)
    assert list(backend.filter_users_by_claims(claims(email_verified=False))) == []
    assert list(backend.filter_users_by_claims(claims())) == [invited]
    backend.update_user(invited, claims(name="Ada Lovelace"))
    invited.refresh_from_db()
    assert invited.oidc_subject == "subject-1" and invited.display_name == "Ada Lovelace"
    assert list(backend.filter_users_by_claims(claims(email="other@example.invalid"))) == [invited]


@pytest.mark.parametrize("value", [1, ["true"], {"verified": True}])
def test_email_verification_rejects_malformed_claim_types(value):
    assert not auth.email_verified(claims(email_verified=value))


def test_first_sign_in_rechecks_the_current_identity():
    backend = auth.OIDCBackend()
    invited = User.objects.create_user(email="ada@example.invalid", role=Role.APPROVER)
    stale = User.objects.get(pk=invited.pk)
    backend.update_user(invited, claims())

    # The earlier match still has no subject when the first sign-in commits.
    with pytest.raises(SuspiciousOperation, match="identity changed"):
        backend.update_user(stale, claims(sub="subject-2"))
    invited.refresh_from_db()
    assert invited.oidc_subject == "subject-1"


def test_unknown_people_get_no_user():
    assert auth.OIDCBackend().create_user(claims()) is None
    assert not User.objects.exists()


def test_bootstrap_admin_only_while_no_admin_exists(settings):
    settings.BOOTSTRAP_ADMIN_EMAILS = ["ada@example.invalid"]
    backend = auth.OIDCBackend()
    assert backend.create_user(claims(email_verified=False)) is None
    user = backend.create_user(claims())
    assert user.role == Role.ADMIN and user.role_source == User.RoleSource.LOCAL
    settings.BOOTSTRAP_ADMIN_EMAILS = ["ada@example.invalid", "eve@example.invalid"]
    assert backend.create_user(claims(sub="s2", email="eve@example.invalid")) is None


def test_group_claims_grant_and_revoke_roles(settings):
    settings.OIDC_ADMIN_GROUPS = ["/iam/admins"]
    settings.OIDC_VIEWER_GROUPS = ["iam-viewers"]
    backend = auth.OIDCBackend()
    user = backend.create_user(claims(groups=["/staff", "/iam/admins"]))
    assert user.role == Role.ADMIN and user.role_source == User.RoleSource.PROVIDER
    backend.update_user(user, claims(groups=["/iam-viewers"]))
    assert user.role == Role.VIEWER
    backend.update_user(user, claims(groups=["iam-viewers"]))
    assert user.role == Role.VIEWER
    backend.update_user(user, claims(groups=[]))
    user.refresh_from_db()
    assert not user.is_active


def test_a_group_name_does_not_match_a_group_further_down(settings):
    # Otherwise everyone in any "/team/admins" would become an admin of this app.
    settings.OIDC_ADMIN_GROUPS = ["admins", "/iam/admins"]
    for groups in (["/sales/admins"], ["/sales/iam/admins"], ["/iam/admins/eu"], ["/iam"]):
        assert auth.role_from_groups(claims(groups=groups)) is None, groups
    for groups in (["admins"], ["/admins"], ["/iam/admins"]):
        assert auth.role_from_groups(claims(groups=groups)) == Role.ADMIN, groups


def test_local_roles_are_not_changed_by_group_claims(settings):
    settings.OIDC_ADMIN_GROUPS = ["admins"]
    user = User.objects.create_user(email="ada@example.invalid", role=Role.APPROVER)
    auth.OIDCBackend().update_user(user, claims(groups=[]))
    user.refresh_from_db()
    assert user.is_active and user.role == Role.APPROVER


def test_tokens_from_another_issuer_or_client_are_refused(monkeypatch):
    backend = auth.OIDCBackend()
    from mozilla_django_oidc.auth import OIDCAuthenticationBackend

    payload = {"iss": ISSUER, "aud": "iam", "sub": "x", "exp": time.time() + 300}
    monkeypatch.setattr(
        OIDCAuthenticationBackend, "verify_token", lambda self, token, **kw: dict(payload)
    )
    assert backend.verify_token("t")["sub"] == "x"
    payload["aud"] = ["other", "iam"]
    assert backend.verify_token("t")
    payload["aud"] = "other"
    with pytest.raises(SuspiciousOperation):
        backend.verify_token("t")
    payload.update(aud="iam", iss="https://evil.example.invalid")
    with pytest.raises(SuspiciousOperation):
        backend.verify_token("t")
    payload.update(iss=ISSUER, exp=time.time() - 30)
    assert backend.verify_token("t"), "within the allowed clock difference"
    for expiry in (time.time() - 3600, None, "tomorrow"):
        payload["exp"] = expiry
        with pytest.raises(SuspiciousOperation):
            backend.verify_token("t")


def test_signed_id_tokens_allow_the_clock_difference(monkeypatch):
    # The library checks exp, iat and nbf with PyJWT, which allows no clock
    # difference: a provider clock a second ahead would refuse sign-ins.
    import jwt
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    other_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key(), as_dict=True)

    def fake_get(url, **kwargs):
        if url == DISCOVERY["jwks_uri"]:
            # No "alg": it is optional in a JWKS, and PyJWT then assumes RS256.
            return FakeResponse({"keys": [{**jwk, "kid": "k1"}]})
        return FakeResponse(DISCOVERY)

    monkeypatch.setattr(auth.requests, "get", fake_get)

    def token(signing_key=key, algorithm="RS256", **extra):
        now = int(time.time())
        claims = {"iss": ISSUER, "aud": "iam", "sub": "x", "nonce": "n", "iat": now}
        claims = {**claims, "exp": now + 300, **extra}
        return jwt.encode(claims, signing_key, algorithm=algorithm, headers={"kid": "k1"})

    backend = auth.OIDCBackend()
    ahead = int(time.time()) + 30
    assert backend.verify_token(token(), nonce="n")["sub"] == "x"
    assert backend.verify_token(token(iat=ahead, nbf=ahead), nonce="n")
    assert backend.verify_token(token(exp=int(time.time()) - 30), nonce="n")
    for refused in (
        token(exp=int(time.time()) - 3600),
        token(iat=int(time.time()) + 3600),
        token(signing_key=other_key),
        token(algorithm="RS512"),
        "",
    ):
        with pytest.raises(SuspiciousOperation):
            backend.verify_token(refused, nonce="n")
    backend.OIDC_RP_SIGN_ALGO = "RS512"
    assert backend.verify_token(token(algorithm="RS512"), nonce="n")


def test_user_info_must_be_about_the_subject_of_the_id_token(monkeypatch):
    from mozilla_django_oidc.auth import OIDCAuthenticationBackend

    info = {"sub": "subject-1", "email": "ada@example.invalid"}
    monkeypatch.setattr(OIDCAuthenticationBackend, "get_userinfo", lambda self, *args: info)
    backend = auth.OIDCBackend()
    assert backend.get_userinfo("access", "id", {"sub": "subject-1"}) == info
    with pytest.raises(SuspiciousOperation):
        backend.get_userinfo("access", "id", {"sub": "someone-else"})


def test_inactive_users_are_signed_out():
    user = User.objects.create_user(email="ada@example.invalid", is_active=False)
    assert auth.OIDCBackend().get_user(user.pk) is None


def test_logout_url_ends_the_provider_session():
    request = RequestFactory().post("/logout/")
    request.session = {"oidc_id_token": "id-token"}
    url = auth.provider_logout_url(request)
    assert url.startswith(f"{ISSUER}/protocol/openid-connect/logout?")
    assert "id_token_hint=id-token" in url and "client_id=iam" in url


def test_logout_keeps_the_providers_existing_query(monkeypatch):
    document = {**DISCOVERY, "end_session_endpoint": f"{ISSUER}/logout?tenant=team"}
    monkeypatch.setattr(auth.requests, "get", lambda url, timeout: FakeResponse(document))
    request = RequestFactory().post("/logout/")
    request.session = {"oidc_id_token": "id-token"}
    query = parse_qs(urlsplit(auth.provider_logout_url(request)).query)
    assert query["tenant"] == ["team"]
    assert query["client_id"] == ["iam"]
    assert query["id_token_hint"] == ["id-token"]


def test_sign_in_redirects_to_the_provider(client):
    response = client.get("/oidc/authenticate/")
    assert response.status_code == 302
    assert response["Location"].startswith(DISCOVERY["authorization_endpoint"])
    assert "code_challenge=" in response["Location"]


def test_sign_in_shows_a_page_when_the_provider_is_down(client, monkeypatch):
    import requests

    def down(url, timeout):
        raise requests.ConnectionError("down")

    monkeypatch.setattr(auth.requests, "get", down)
    cache.clear()
    response = client.get("/oidc/authenticate/")
    assert response.status_code == 503
    assert b"cannot be reached" in response.content


def test_pages_need_sign_in(client):
    response = client.get("/people/")
    assert response.status_code == 302 and response["Location"].startswith("/login/")
    assert client.get("/login/").status_code == 200
    assert client.get("/-/healthz").status_code == 200


def test_sign_in_error_without_matching_state_keeps_the_current_session(client_for, admin_user):
    client = client_for(admin_user)
    session = client.session
    session["oidc_states"] = {"pending": {"nonce": "sample-nonce"}}
    session.save()

    response = client.get("/oidc/callback/", {"error": "access_denied", "state": "unrelated"})
    assert response.status_code == 302
    assert response.url == "/login/denied/"
    assert client.session["_auth_user_id"] == str(admin_user.pk)
    assert "pending" in client.session["oidc_states"]


def test_dev_login_is_off_unless_enabled(client):
    assert client.get("/login/dev/").status_code == 404


def test_providers_without_discovery_work_with_all_endpoints_set(settings, monkeypatch):
    import requests

    def down(url, timeout):
        raise requests.ConnectionError("no discovery document")

    monkeypatch.setattr(auth.requests, "get", down)
    settings.OIDC_OP_AUTHORIZATION_ENDPOINT = f"{ISSUER}/auth"
    settings.OIDC_OP_TOKEN_ENDPOINT = f"{ISSUER}/token"
    settings.OIDC_OP_USER_ENDPOINT = f"{ISSUER}/userinfo"
    settings.OIDC_OP_JWKS_ENDPOINT = f"{ISSUER}/certs"
    assert auth.discovery()["token_endpoint"] == f"{ISSUER}/token"
    settings.OIDC_OP_JWKS_ENDPOINT = ""
    with pytest.raises(auth.ProviderUnavailable):
        auth.discovery()


def test_endpoint_settings_keep_the_sign_out_of_a_discovery_document(settings):
    settings.OIDC_OP_AUTHORIZATION_ENDPOINT = f"{ISSUER}/auth"
    settings.OIDC_OP_TOKEN_ENDPOINT = f"{ISSUER}/token"
    settings.OIDC_OP_USER_ENDPOINT = f"{ISSUER}/userinfo"
    settings.OIDC_OP_JWKS_ENDPOINT = f"{ISSUER}/certs"
    document = auth.discovery()
    assert document["token_endpoint"] == f"{ISSUER}/token"
    assert document["end_session_endpoint"] == DISCOVERY["end_session_endpoint"]


@pytest.mark.parametrize("decode_error", [ValueError, RecursionError])
def test_explicit_endpoints_do_not_mask_malformed_discovery_json(
    client, settings, monkeypatch, decode_error
):
    class InvalidJson(FakeResponse):
        def json(self):
            raise decode_error("invalid JSON")

    settings.OIDC_OP_AUTHORIZATION_ENDPOINT = f"{ISSUER}/auth"
    settings.OIDC_OP_TOKEN_ENDPOINT = f"{ISSUER}/token"
    settings.OIDC_OP_USER_ENDPOINT = f"{ISSUER}/userinfo"
    settings.OIDC_OP_JWKS_ENDPOINT = f"{ISSUER}/certs"
    monkeypatch.setattr(auth.requests, "get", lambda url, timeout: InvalidJson(None))

    assert client.get("/oidc/authenticate/").status_code == 503


def test_role_changes_from_group_claims_are_audited(settings):
    from iam.models import AuditEvent

    settings.OIDC_ADMIN_GROUPS = ["/iam/admins"]
    settings.OIDC_VIEWER_GROUPS = ["/iam/viewers"]
    backend = auth.OIDCBackend()
    user = backend.create_user(claims(groups=["/iam/viewers"]))
    backend.update_user(user, claims(groups=["/iam/admins"]))
    promoted = AuditEvent.objects.get(action="user.signed_in")
    assert promoted.details["role"] == ["viewer", "admin"]
    backend.update_user(user, claims(groups=[]))
    turned_off = AuditEvent.objects.filter(action="user.signed_in").latest("id")
    assert turned_off.details["is_active"] == [True, False]
    assert "subject-1" not in str(turned_off.details)
    backend.update_user(user, claims(groups=[]))  # signing in again changes nothing
    assert AuditEvent.objects.filter(action="user.signed_in").count() == 2

"""Sign-in with OpenID Connect (any provider) or Keycloak.

The provider proves who someone is; the app's own user records decide what
they may do. A sign-in succeeds only for an active app user, found by the
provider's subject or, for a user an admin added by email, by a verified
email address (the subject is then bound). A user can also get a role from
the provider's group claim (OIDC_*_GROUPS) or, while no admin exists, from
BOOTSTRAP_ADMIN_EMAILS.

Endpoints come from the provider's discovery document, fetched on first use
and cached, so the app starts even while the provider is down.
"""

from __future__ import annotations

import logging
import time
from urllib.parse import urlencode

import requests
from django.conf import settings
from django.contrib.auth.backends import BaseBackend
from django.core.cache import cache
from django.core.exceptions import ImproperlyConfigured, SuspiciousOperation
from django.shortcuts import render
from django.urls import reverse
from mozilla_django_oidc.auth import OIDCAuthenticationBackend
from mozilla_django_oidc.views import OIDCAuthenticationCallbackView, OIDCAuthenticationRequestView

from . import audit
from .models import Role, User

logger = logging.getLogger(__name__)

DISCOVERY_CACHE_SECONDS = 3600
# Allowed difference between this server's clock and the provider's.
CLOCK_SKEW_SECONDS = 60


class ProviderUnavailable(Exception):
    pass


def discovery() -> dict:
    """The provider's OpenID configuration, with explicit settings winning."""
    if not settings.OIDC_ISSUER:
        raise ImproperlyConfigured("Set OIDC_ISSUER, or KEYCLOAK_URL and KEYCLOAK_REALM.")
    overrides = {
        "authorization_endpoint": settings.OIDC_OP_AUTHORIZATION_ENDPOINT,
        "token_endpoint": settings.OIDC_OP_TOKEN_ENDPOINT,
        "userinfo_endpoint": settings.OIDC_OP_USER_ENDPOINT,
        "jwks_uri": settings.OIDC_OP_JWKS_ENDPOINT,
    }
    if all(overrides.values()):
        # Every endpoint is set: a provider without a discovery document.
        return {"issuer": settings.OIDC_ISSUER, **overrides}
    key = f"oidc-discovery:{settings.OIDC_ISSUER}"
    document = cache.get(key)
    if document is None:
        url = f"{settings.OIDC_ISSUER}/.well-known/openid-configuration"
        try:
            response = requests.get(url, timeout=settings.OIDC_TIMEOUT)
            response.raise_for_status()
            document = response.json()
        except (requests.RequestException, ValueError) as exc:
            raise ProviderUnavailable(f"Could not read {url}") from exc
        if document.get("issuer", "").rstrip("/") != settings.OIDC_ISSUER:
            raise ProviderUnavailable("The discovery document names a different issuer.")
        cache.set(key, document, DISCOVERY_CACHE_SECONDS)
    return {**document, **{k: v for k, v in overrides.items() if v}}


def provider_logout_url(request) -> str:
    post_logout = request.build_absolute_uri(reverse("login"))
    try:
        endpoint = discovery().get("end_session_endpoint")
    except (ProviderUnavailable, ImproperlyConfigured):
        endpoint = None
    if not endpoint:
        return post_logout
    query = {"client_id": settings.OIDC_RP_CLIENT_ID, "post_logout_redirect_uri": post_logout}
    id_token = request.session.get("oidc_id_token")
    if id_token:
        query["id_token_hint"] = id_token
    return f"{endpoint}?{urlencode(query)}"


def account_url() -> str:
    """Where people manage their own sign-in (Keycloak's account console)."""
    if settings.OIDC_PROVIDER_KIND == "keycloak" and settings.OIDC_ISSUER:
        return f"{settings.OIDC_ISSUER}/account/"
    return ""


def email_verified(claims: dict) -> bool:
    value = claims.get("email_verified")
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes"}
    return bool(value)


def claim_groups(claims: dict) -> set[str]:
    groups = claims.get(settings.OIDC_GROUPS_CLAIM) or []
    if isinstance(groups, str):
        groups = [groups]
    if not isinstance(groups, list):
        return set()
    return {group for group in groups if isinstance(group, str)}


def group_matches(name: str, groups: set[str]) -> bool:
    """Whether a configured group is among the claimed groups. Keycloak sends
    full paths ("/iam/admins") when the mapper has "Full group path" on, so a
    name without a slash also matches that top-level group ("/admins").
    Security: it never matches a group of that name further down, or everyone
    in any "/team/admins" would get the role."""
    return name in groups or ("/" not in name and f"/{name}" in groups)


def role_from_groups(claims: dict) -> str | None:
    groups = claim_groups(claims)
    for role, names in (
        (Role.ADMIN, settings.OIDC_ADMIN_GROUPS),
        (Role.APPROVER, settings.OIDC_APPROVER_GROUPS),
        (Role.VIEWER, settings.OIDC_VIEWER_GROUPS),
    ):
        if any(group_matches(name, groups) for name in names):
            return role
    return None


def group_roles_configured() -> bool:
    return bool(
        settings.OIDC_ADMIN_GROUPS or settings.OIDC_APPROVER_GROUPS or settings.OIDC_VIEWER_GROUPS
    )


class OIDCBackend(OIDCAuthenticationBackend):
    def __init__(self, *args, **kwargs):
        self.OIDC_RP_CLIENT_ID = settings.OIDC_RP_CLIENT_ID
        self.OIDC_RP_CLIENT_SECRET = settings.OIDC_RP_CLIENT_SECRET
        self.OIDC_RP_SIGN_ALGO = settings.OIDC_RP_SIGN_ALGO
        self.OIDC_RP_IDP_SIGN_KEY = None
        self.UserModel = User
        self._endpoints = None

    def _endpoint(self, name: str) -> str:
        if self._endpoints is None:
            self._endpoints = discovery()
        return self._endpoints.get(name, "")

    @property
    def OIDC_OP_TOKEN_ENDPOINT(self):  # noqa: N802 - name used by the library
        return self._endpoint("token_endpoint")

    @property
    def OIDC_OP_USER_ENDPOINT(self):  # noqa: N802
        return self._endpoint("userinfo_endpoint")

    @property
    def OIDC_OP_JWKS_ENDPOINT(self):  # noqa: N802
        return self._endpoint("jwks_uri")

    def verify_token(self, token, **kwargs):
        payload = super().verify_token(token, **kwargs)
        if payload.get("iss", "").rstrip("/") != settings.OIDC_ISSUER:
            raise SuspiciousOperation("ID token from an unexpected issuer")
        audience = payload.get("aud")
        audiences = audience if isinstance(audience, list) else [audience]
        if settings.OIDC_RP_CLIENT_ID not in audiences:
            raise SuspiciousOperation("ID token for another client")
        # Security: the library does not check the expiry (OpenID Connect Core 3.1.3.7).
        expires = payload.get("exp")
        if isinstance(expires, bool) or not isinstance(expires, int | float):
            raise SuspiciousOperation("ID token without an expiry")
        if expires < time.time() - CLOCK_SKEW_SECONDS:
            raise SuspiciousOperation("ID token has expired")
        return payload

    def get_userinfo(self, access_token, id_token, payload):
        claims = super().get_userinfo(access_token, id_token, payload)
        # Security: the user is chosen from these claims, so they must be about
        # the subject of the verified ID token (OpenID Connect Core 5.3.2).
        if not isinstance(claims, dict) or claims.get("sub") != payload.get("sub"):
            raise SuspiciousOperation("User info for another subject than the ID token")
        return claims

    def verify_claims(self, claims):
        return bool(claims.get("sub"))

    def filter_users_by_claims(self, claims):
        subject = claims["sub"]
        users = User.objects.filter(oidc_subject=subject)
        if users.exists():
            return users
        email = (claims.get("email") or "").strip().lower()
        if email and email_verified(claims):
            return User.objects.filter(email__iexact=email, oidc_subject__isnull=True)
        return User.objects.none()

    def create_user(self, claims):
        email = (claims.get("email") or "").strip().lower()
        if not email or not email_verified(claims):
            return None
        role = role_from_groups(claims)
        source = User.RoleSource.PROVIDER
        if role is None and email in settings.BOOTSTRAP_ADMIN_EMAILS:
            if User.objects.filter(role=Role.ADMIN, is_active=True).exists():
                return None
            role, source = Role.ADMIN, User.RoleSource.LOCAL
        if role is None or User.objects.filter(email__iexact=email).exists():
            return None
        user = User.objects.create_user(
            email=email,
            role=role,
            role_source=source,
            display_name=self._name(claims),
            oidc_subject=claims["sub"],
        )
        audit.record(
            "user.created",
            f"Created app user {email} ({user.get_role_display()}) at first sign-in",
            actor=user,
            target=user,
            details={"role_source": source},
        )
        return user

    def update_user(self, user, claims):
        changed = []
        before = {"role": user.role, "is_active": user.is_active}
        if user.oidc_subject != claims["sub"]:
            user.oidc_subject = claims["sub"]
            changed.append("oidc_subject")
        name = self._name(claims)
        if name and name != user.display_name:
            user.display_name = name
            changed.append("display_name")
        if group_roles_configured() and user.role_source == User.RoleSource.PROVIDER:
            role = role_from_groups(claims)
            if role is None:
                user.is_active = False
                changed.append("is_active")
            elif role != user.role:
                user.role = role
                changed.append("role")
        if changed:
            user.save(update_fields=changed)
            self._audit_update(user, changed, before)
        return user

    @staticmethod
    def _audit_update(user, changed: list[str], before: dict) -> None:
        details = {"fields": sorted(changed)}
        for name in ("role", "is_active"):
            if name in changed:
                details[name] = [before[name], getattr(user, name)]
        if "is_active" in changed:
            summary = f"Turned off app user {user.email}: no group claim grants a role"
        elif "role" in changed:
            summary = (
                f"Changed the role of app user {user.email} to "
                f"{user.get_role_display().lower()} from a group claim"
            )
        else:
            summary = f"Updated app user {user.email} at sign-in"
        audit.record("user.signed_in", summary, actor=user, target=user, details=details)

    @staticmethod
    def _name(claims) -> str:
        name = claims.get("name") or " ".join(
            filter(None, [claims.get("given_name"), claims.get("family_name")])
        )
        return (name or "")[:200]

    def user_can_authenticate(self, user):
        return user.is_active

    def get_user(self, user_id):
        try:
            user = User.objects.get(pk=user_id)
        except User.DoesNotExist:
            return None
        return user if user.is_active else None


class AuthenticationRequestView(OIDCAuthenticationRequestView):
    def __init__(self, *args, **kwargs):
        super(OIDCAuthenticationRequestView, self).__init__(*args, **kwargs)
        self.OIDC_RP_CLIENT_ID = settings.OIDC_RP_CLIENT_ID

    def get(self, request):
        try:
            self.OIDC_OP_AUTH_ENDPOINT = discovery()["authorization_endpoint"]
        except (ProviderUnavailable, ImproperlyConfigured, KeyError):
            logger.warning("Sign-in provider unavailable", exc_info=True)
            return render(request, "iam/login_unavailable.html", status=503)
        return super().get(request)


class CallbackView(OIDCAuthenticationCallbackView):
    def get(self, request):
        try:
            return super().get(request)
        except ProviderUnavailable:
            return render(request, "iam/login_unavailable.html", status=503)
        except requests.RequestException:
            logger.warning("Sign-in provider request failed", exc_info=False)
            return render(request, "iam/login_unavailable.html", status=503)

    def login_success(self):
        response = super().login_success()
        # The library skips login() for a repeated sign-in of the same user;
        # rotate the session key on every successful sign-in anyway.
        self.request.session.cycle_key()
        return response


class DevLoginBackend(BaseBackend):
    """Development only (DEV_LOGIN=1 with DJANGO_DEBUG=1): sign in as an
    existing app user without a provider."""

    def authenticate(self, request, dev_email=None, **kwargs):
        if dev_email is None or not (settings.DEV_LOGIN or settings.DEBUG and _testing()):
            return None
        return User.objects.filter(email__iexact=dev_email, is_active=True).first()

    def get_user(self, user_id):
        return User.objects.filter(pk=user_id, is_active=True).first()


def _testing() -> bool:
    return settings.SETTINGS_MODULE == "config.test_settings"

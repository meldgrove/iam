"""Deployment settings. Everything an operator changes at runtime (connections,
lifecycle rules, organization name) lives in the database and the admin area;
this file only reads what the process needs before it can reach the database."""

import os
from pathlib import Path

import dj_database_url
from django.core.exceptions import ImproperlyConfigured

BASE_DIR = Path(__file__).resolve().parent.parent


def env_list(name: str, default: str = "") -> list[str]:
    return [item.strip() for item in os.getenv(name, default).split(",") if item.strip()]


# Set in release images (Dockerfile); empty in images built from source.
APP_VERSION = os.getenv("APP_VERSION", "")
DEBUG = os.getenv("DJANGO_DEBUG", "0") == "1"
DEPLOYMENT_ENV = os.getenv("DEPLOYMENT_ENV", "development" if DEBUG else "production")
if DEPLOYMENT_ENV not in {"development", "staging", "production"} or DEBUG != (
    DEPLOYMENT_ENV == "development"
):
    raise ImproperlyConfigured("Only DEPLOYMENT_ENV=development may enable DJANGO_DEBUG.")

SECRET_KEY = os.getenv(
    "DJANGO_SECRET_KEY", "development-only-do-not-use-in-production" if DEBUG else ""
)
# Fernet keys that encrypt connection secrets. The first key encrypts; all keys
# decrypt, so a new key can be put in front of the old one to rotate.
ENCRYPTION_KEYS = env_list("ENCRYPTION_KEY")
ALLOWED_HOSTS = env_list("ALLOWED_HOSTS", "localhost,127.0.0.1,testserver" if DEBUG else "")
CSRF_TRUSTED_ORIGINS = env_list("CSRF_TRUSTED_ORIGINS")
ROOT_URLCONF = "config.urls"
WSGI_APPLICATION = "config.wsgi.application"

INSTALLED_APPS = [
    "iam",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "mozilla_django_oidc",
]
MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.auth.middleware.LoginRequiredMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "iam.middleware.PrivateResponseMiddleware",
]
TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "iam.context_processors.app",
            ]
        },
    }
]

DATABASES = {
    "default": dj_database_url.parse(
        os.getenv("DATABASE_URL", f"sqlite:///{BASE_DIR / 'var/development.sqlite3'}"),
        conn_max_age=60,
    )
}
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
AUTH_USER_MODEL = "iam.User"

# Sign-in: any OpenID Connect provider, or Keycloak by base URL and realm
# (docs/authentication.md). Authorization stays in the app: a person signs in
# only with an active app user, created in Admin > Users or granted by a group
# claim (OIDC_*_GROUPS).
KEYCLOAK_URL = os.getenv("KEYCLOAK_URL", "").rstrip("/")
KEYCLOAK_REALM = os.getenv("KEYCLOAK_REALM", "")
if KEYCLOAK_URL and KEYCLOAK_REALM:
    OIDC_ISSUER = f"{KEYCLOAK_URL}/realms/{KEYCLOAK_REALM}"
    OIDC_PROVIDER_KIND = "keycloak"
else:
    OIDC_ISSUER = os.getenv("OIDC_ISSUER", "").rstrip("/")
    OIDC_PROVIDER_KIND = "keycloak" if "/realms/" in OIDC_ISSUER else "oidc"
OIDC_PROVIDER_NAME = os.getenv(
    "OIDC_PROVIDER_NAME", "Keycloak" if OIDC_PROVIDER_KIND == "keycloak" else "single sign-on"
)
OIDC_RP_CLIENT_ID = os.getenv("OIDC_CLIENT_ID", "")
OIDC_RP_CLIENT_SECRET = os.getenv("OIDC_CLIENT_SECRET", "")
OIDC_ENABLED = bool(OIDC_ISSUER and OIDC_RP_CLIENT_ID)
OIDC_RP_SIGN_ALGO = os.getenv("OIDC_SIGN_ALGO", "RS256")
OIDC_RP_SCOPES = os.getenv("OIDC_SCOPES", "openid email profile")
OIDC_USE_PKCE = True
OIDC_PKCE_CODE_CHALLENGE_METHOD = "S256"
OIDC_STORE_ACCESS_TOKEN = False
# The ID token is the logout hint; without it the provider keeps its session.
OIDC_STORE_ID_TOKEN = True
OIDC_TIMEOUT = (3, 10)
OIDC_GROUPS_CLAIM = os.getenv("OIDC_GROUPS_CLAIM", "groups")
OIDC_ADMIN_GROUPS = env_list("OIDC_ADMIN_GROUPS")
OIDC_APPROVER_GROUPS = env_list("OIDC_APPROVER_GROUPS")
OIDC_VIEWER_GROUPS = env_list("OIDC_VIEWER_GROUPS")
# Verified addresses that may claim the first admin account while no active
# admin exists (docs/authentication.md).
BOOTSTRAP_ADMIN_EMAILS = [email.lower() for email in env_list("BOOTSTRAP_ADMIN_EMAILS")]
# The discovery document is fetched lazily so a provider outage never stops
# the app from starting; these override discovery when a provider has none.
OIDC_OP_AUTHORIZATION_ENDPOINT = os.getenv("OIDC_AUTHORIZATION_ENDPOINT", "")
OIDC_OP_TOKEN_ENDPOINT = os.getenv("OIDC_TOKEN_ENDPOINT", "")
OIDC_OP_USER_ENDPOINT = os.getenv("OIDC_USERINFO_ENDPOINT", "")
OIDC_OP_JWKS_ENDPOINT = os.getenv("OIDC_JWKS_ENDPOINT", "")
OIDC_OP_LOGOUT_URL_METHOD = "iam.auth.provider_logout_url"
OIDC_CALLBACK_CLASS = "iam.auth.CallbackView"
OIDC_AUTHENTICATE_CLASS = "iam.auth.AuthenticationRequestView"
AUTHENTICATION_BACKENDS = ["iam.auth.OIDCBackend"]
# Local development without an identity provider (docs/development.md).
DEV_LOGIN = DEBUG and os.getenv("DEV_LOGIN", "0") == "1"
if DEV_LOGIN:
    AUTHENTICATION_BACKENDS.append("iam.auth.DevLoginBackend")
LOGIN_URL = "login"
LOGIN_REDIRECT_URL = "/"
LOGIN_REDIRECT_URL_FAILURE = "/login/denied/"
LOGOUT_REDIRECT_URL = "/login/"

LANGUAGE_CODE = "en"
TIME_ZONE = os.getenv("TZ", "UTC")
USE_I18N = False
USE_TZ = True
STATIC_URL = "/static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {
        "BACKEND": (
            "django.contrib.staticfiles.storage.StaticFilesStorage"
            if DEBUG
            else "whitenoise.storage.CompressedManifestStaticFilesStorage"
        )
    },
}

SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"
SESSION_COOKIE_AGE = int(os.getenv("SESSION_HOURS", "12")) * 3600
SESSION_COOKIE_SECURE = not DEBUG
CSRF_COOKIE_SECURE = not DEBUG
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = "same-origin"
SECURE_SSL_REDIRECT = not DEBUG and os.getenv("SECURE_SSL_REDIRECT", "1") == "1"
SECURE_REDIRECT_EXEMPT = [r"^-/"]
SECURE_HSTS_SECONDS = 31536000 if not DEBUG else 0
SECURE_HSTS_INCLUDE_SUBDOMAINS = os.getenv("SECURE_HSTS_INCLUDE_SUBDOMAINS", "1") == "1"
# Preloading is a decision for the domain's owner, not for this app.
SILENCED_SYSTEM_CHECKS = ["security.W021"]
# Enable only behind a reverse proxy that replaces this header.
if os.getenv("TRUST_PROXY_HEADERS", "0") == "1":
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
X_FRAME_OPTIONS = "DENY"
DATA_UPLOAD_MAX_MEMORY_SIZE = 256 * 1024
DATA_UPLOAD_MAX_NUMBER_FIELDS = 2000

# The worker (manage.py run_worker) runs provisioning jobs and the schedule.
WORKER_POLL_SECONDS = float(os.getenv("WORKER_POLL_SECONDS", "5"))
WORKER_HEARTBEAT_MAX_AGE_SECONDS = int(os.getenv("WORKER_HEARTBEAT_MAX_AGE_SECONDS", "300"))
JOB_LEASE_SECONDS = int(os.getenv("JOB_LEASE_SECONDS", "300"))
HEALTHCHECK_TOKEN = os.getenv("HEALTHCHECK_TOKEN", "")
# Outbound calls to connected apps.
PROVIDER_TIMEOUT_SECONDS = float(os.getenv("PROVIDER_TIMEOUT_SECONDS", "20"))

SENTRY_DSN = os.getenv("SENTRY_DSN", "")
SENTRY_ENVIRONMENT = os.getenv("SENTRY_ENVIRONMENT", DEPLOYMENT_ENV)
SENTRY_RELEASE = os.getenv("SENTRY_RELEASE", f"iam@{APP_VERSION}" if APP_VERSION else "")

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {"plain": {"format": "{levelname} {name} {message}", "style": "{"}},
    "handlers": {"console": {"class": "logging.StreamHandler", "formatter": "plain"}},
    "root": {"handlers": ["console"], "level": "INFO"},
    "loggers": {"httpx": {"level": "WARNING"}, "httpcore": {"level": "WARNING"}},
}

if not DEBUG:
    missing = [
        name
        for name, value in {
            "DJANGO_SECRET_KEY": SECRET_KEY,
            "ENCRYPTION_KEY": ENCRYPTION_KEYS,
            "ALLOWED_HOSTS": ALLOWED_HOSTS,
        }.items()
        if not value
    ]
    if missing:
        raise ImproperlyConfigured(f"Set {', '.join(missing)}.")
    if len(SECRET_KEY) < 50:
        raise ImproperlyConfigured("DJANGO_SECRET_KEY must have at least 50 characters.")

if SENTRY_DSN:
    import sentry_sdk
    from sentry_sdk.integrations.django import DjangoIntegration

    sentry_sdk.init(
        dsn=SENTRY_DSN,
        environment=SENTRY_ENVIRONMENT,
        release=SENTRY_RELEASE or None,
        integrations=[DjangoIntegration()],
        send_default_pii=False,
        traces_sample_rate=float(os.getenv("SENTRY_TRACES_SAMPLE_RATE", "0")),
    )

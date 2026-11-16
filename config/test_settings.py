import os

os.environ.setdefault("DJANGO_DEBUG", "1")
os.environ.setdefault("DEPLOYMENT_ENV", "development")

from config.settings import *  # noqa: E402,F403

DATABASES = {
    "default": __import__("dj_database_url").parse(
        os.getenv("TEST_DATABASE_URL", "sqlite://:memory:")
    )
}
ENCRYPTION_KEYS = ["dGVzdC1lbmNyeXB0aW9uLWtleS0zMi1ieXRlcy0wMDA="]
PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]
OIDC_ISSUER = "https://sso.example.invalid/realms/test"
OIDC_PROVIDER_KIND = "keycloak"
OIDC_PROVIDER_NAME = "Keycloak"
OIDC_RP_CLIENT_ID = "iam"
OIDC_RP_CLIENT_SECRET = "test-client-secret"
OIDC_ENABLED = True
DEV_LOGIN = False
AUTHENTICATION_BACKENDS = ["iam.auth.OIDCBackend", "iam.auth.DevLoginBackend"]
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
}
SECURE_SSL_REDIRECT = False

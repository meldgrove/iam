"""Encryption of connection secrets at rest (Fernet: AES-128-CBC with HMAC).

ENCRYPTION_KEY holds one or more comma-separated Fernet keys. The first key
encrypts; every key decrypts, so rotation is: put a new key first, run
`manage.py rotate_secrets`, then drop the old key."""

from __future__ import annotations

import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken, MultiFernet
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured


class SecretUnreadable(Exception):
    """A stored secret was encrypted with a key that is no longer configured."""


def _keys() -> list[bytes]:
    keys = [key.encode() for key in settings.ENCRYPTION_KEYS]
    if keys:
        return keys
    if not settings.DEBUG:
        raise ImproperlyConfigured("Set ENCRYPTION_KEY to encrypt connection secrets.")
    # Development only: derived from the development secret key.
    digest = hashlib.sha256(f"iam-dev:{settings.SECRET_KEY}".encode()).digest()
    return [base64.urlsafe_b64encode(digest)]


def _fernet() -> MultiFernet:
    try:
        return MultiFernet([Fernet(key) for key in _keys()])
    except ValueError as exc:
        raise ImproperlyConfigured(
            "ENCRYPTION_KEY must be Fernet keys: 32 url-safe base64-encoded bytes each."
        ) from exc


def encrypt(value: str) -> str:
    return _fernet().encrypt(value.encode()).decode()


def decrypt(token: str) -> str:
    try:
        return _fernet().decrypt(token.encode()).decode()
    except InvalidToken as exc:
        raise SecretUnreadable(
            "A stored secret cannot be decrypted with the configured ENCRYPTION_KEY."
        ) from exc


def rotate(token: str) -> str:
    return _fernet().rotate(token.encode()).decode()


def generate_key() -> str:
    return Fernet.generate_key().decode()

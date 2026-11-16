import pytest
from cryptography.fernet import Fernet
from django.core.exceptions import ImproperlyConfigured
from django.core.management import call_command

from iam import crypto
from iam.models import Connection

pytestmark = pytest.mark.django_db


def test_round_trip_and_ciphertext_differs():
    token = crypto.encrypt("secret")
    assert token != "secret" and "secret" not in token
    assert crypto.decrypt(token) == "secret"


def test_wrong_key_is_reported(settings):
    token = crypto.encrypt("secret")
    settings.ENCRYPTION_KEYS = [Fernet.generate_key().decode()]
    with pytest.raises(crypto.SecretUnreadable):
        crypto.decrypt(token)


def test_invalid_key_is_a_configuration_error(settings):
    settings.ENCRYPTION_KEYS = ["not-a-key"]
    with pytest.raises(ImproperlyConfigured):
        crypto.encrypt("x")


def test_production_needs_a_key(settings):
    settings.ENCRYPTION_KEYS = []
    settings.DEBUG = False
    with pytest.raises(ImproperlyConfigured):
        crypto.encrypt("x")


def test_rotation_re_encrypts_with_the_new_key(settings):
    old = settings.ENCRYPTION_KEYS[0]
    connection = Connection(adapter="manual", name="App")
    connection.set_secrets({"token": "abc"})
    connection.save()
    new = Fernet.generate_key().decode()
    settings.ENCRYPTION_KEYS = [new, old]
    call_command("rotate_secrets")
    settings.ENCRYPTION_KEYS = [new]
    connection.refresh_from_db()
    assert connection.secrets() == {"token": "abc"}

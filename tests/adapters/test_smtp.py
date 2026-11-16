import smtplib

import pytest

from iam.adapters import smtp as smtp_module
from iam.adapters.base import AdapterError, ConfigurationError, TransientError
from iam.adapters.smtp import SmtpAdapter

CONFIG = {
    "host": "smtp.example.invalid",
    "port": 587,
    "username": "mailer",
    "password": "mail-secret",
    "from_address": "it@example.invalid",
    "from_name": "IT team",
}


class FakeSMTP:
    instances: list["FakeSMTP"] = []
    fail_on: dict[str, BaseException] = {}

    def __init__(self, host, port, timeout=None, context=None):
        self.host, self.port, self.timeout, self.context = host, port, timeout, context
        self.calls = []
        self.sent = []
        FakeSMTP.instances.append(self)
        self._maybe_fail("connect")

    def _maybe_fail(self, step):
        if step in self.fail_on:
            raise self.fail_on[step]

    def starttls(self, context=None):
        self.calls.append("starttls")
        self._maybe_fail("starttls")

    def login(self, username, password):
        self.calls.append(("login", username, password))
        self._maybe_fail("login")

    def noop(self):
        self.calls.append("noop")
        self._maybe_fail("noop")

    def send_message(self, message, from_addr=None, to_addrs=None):
        self.calls.append("send")
        self._maybe_fail("send")
        self.sent.append((message, from_addr, to_addrs))

    def quit(self):
        self.calls.append("quit")

    def close(self):
        self.calls.append("close")


class FakeSMTPSSL(FakeSMTP):
    pass


@pytest.fixture
def fake_smtp(monkeypatch):
    FakeSMTP.instances = []
    FakeSMTP.fail_on = {}
    monkeypatch.setattr(smtp_module.smtplib, "SMTP", FakeSMTP)
    monkeypatch.setattr(smtp_module.smtplib, "SMTP_SSL", FakeSMTPSSL)
    return FakeSMTP


def test_send_uses_starttls_login_and_builds_the_message(fake_smtp, settings):
    settings.PROVIDER_TIMEOUT_SECONDS = 7

    SmtpAdapter(CONFIG).send(
        to=["new.person@example.invalid"],
        subject="Your account",
        body="Welcome aboard.",
        reply_to="help@example.invalid",
    )

    [server] = fake_smtp.instances
    assert (server.host, server.port, server.timeout) == ("smtp.example.invalid", 587, 7)
    assert server.calls == ["starttls", ("login", "mailer", "mail-secret"), "send", "quit"]
    message, from_addr, to_addrs = server.sent[0]
    assert from_addr == "it@example.invalid"
    assert to_addrs == ["new.person@example.invalid"]
    assert message["From"] == "IT team <it@example.invalid>"
    assert message["To"] == "new.person@example.invalid"
    assert message["Subject"] == "Your account"
    assert message["Reply-To"] == "help@example.invalid"
    assert message.get_content().strip() == "Welcome aboard."


def test_ssl_without_login(fake_smtp):
    config = {
        "host": "smtp.example.invalid",
        "port": 465,
        "security": "ssl",
        "from_address": "it@example.invalid",
    }

    SmtpAdapter(config).send(to=["a@example.invalid"], subject="Hi", body="Body")

    [server] = fake_smtp.instances
    assert isinstance(server, FakeSMTPSSL)
    assert server.context is not None
    assert server.calls == ["send", "quit"]


def test_configured_reply_to_is_the_default(fake_smtp):
    SmtpAdapter({**CONFIG, "reply_to": "desk@example.invalid"}).send(
        to=["a@example.invalid"], subject="Hi", body="Body"
    )

    message = fake_smtp.instances[0].sent[0][0]
    assert message["Reply-To"] == "desk@example.invalid"


def test_authentication_failure_is_a_configuration_error(fake_smtp):
    fake_smtp.fail_on = {"login": smtplib.SMTPAuthenticationError(535, b"bad credentials")}

    with pytest.raises(ConfigurationError) as error:
        SmtpAdapter(CONFIG).send(to=["a@example.invalid"], subject="Hi", body="Body")

    assert "mail-secret" not in str(error.value)
    assert fake_smtp.instances[0].calls[-1] == "close"


@pytest.mark.parametrize(
    "failure",
    [
        TimeoutError("timed out"),
        ConnectionRefusedError("refused"),
        smtplib.SMTPServerDisconnected("gone"),
        smtplib.SMTPResponseException(451, b"try later"),
    ],
)
def test_temporary_failures_are_transient(fake_smtp, failure):
    fake_smtp.fail_on = {"send": failure}

    with pytest.raises(TransientError):
        SmtpAdapter(CONFIG).send(to=["a@example.invalid"], subject="Hi", body="Body")


def test_unreachable_server_is_transient(fake_smtp):
    fake_smtp.fail_on = {"connect": OSError("no route")}

    with pytest.raises(TransientError):
        SmtpAdapter(CONFIG).send(to=["a@example.invalid"], subject="Hi", body="Body")


def test_refused_recipient_is_an_adapter_error(fake_smtp):
    fake_smtp.fail_on = {
        "send": smtplib.SMTPRecipientsRefused({"a@example.invalid": (550, b"no such user")})
    }

    with pytest.raises(AdapterError) as error:
        SmtpAdapter(CONFIG).send(to=["a@example.invalid"], subject="Hi", body="Body")

    assert not isinstance(error.value, TransientError)


def test_refused_sender_is_an_adapter_error(fake_smtp):
    fake_smtp.fail_on = {
        "send": smtplib.SMTPSenderRefused(553, b"not allowed", "it@example.invalid")
    }

    with pytest.raises(AdapterError) as error:
        SmtpAdapter(CONFIG).send(to=["a@example.invalid"], subject="Hi", body="Body")

    assert not isinstance(error.value, TransientError)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"to": ["a@example.invalid"], "subject": "Hi\r\nBcc: x@example.invalid", "body": "B"},
        {"to": ["a@example.invalid\nBcc: x@example.invalid"], "subject": "Hi", "body": "B"},
        {
            "to": ["a@example.invalid"],
            "subject": "Hi",
            "body": "B",
            "reply_to": "r@example.invalid\r\nX: y",
        },
    ],
)
def test_header_injection_is_refused_before_connecting(fake_smtp, kwargs):
    with pytest.raises(AdapterError):
        SmtpAdapter(CONFIG).send(**kwargs)

    assert fake_smtp.instances == []


def test_send_without_recipients_is_refused(fake_smtp):
    with pytest.raises(AdapterError):
        SmtpAdapter(CONFIG).send(to=[" "], subject="Hi", body="Body")


def test_connection_test_signs_in_without_sending(fake_smtp):
    result = SmtpAdapter(CONFIG).test_connection()

    [server] = fake_smtp.instances
    assert result.ok
    assert server.calls == ["starttls", ("login", "mailer", "mail-secret"), "noop", "quit"]
    assert server.sent == []


def test_connection_test_requires_host_and_from_address(fake_smtp):
    with pytest.raises(ConfigurationError):
        SmtpAdapter({"host": "smtp.example.invalid"}).test_connection()
    assert fake_smtp.instances == []

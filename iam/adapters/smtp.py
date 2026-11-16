"""Email over SMTP: notifications to admins and sign-in details to new people.

Works with any mail server or sending service that offers SMTP (Mailgun,
MailerSend, Postmark, Amazon SES, Microsoft 365, Google Workspace relay)."""

from __future__ import annotations

import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formataddr

from django.conf import settings

from .base import (
    Adapter,
    AdapterError,
    AdapterKind,
    ConfigurationError,
    Field,
    TestResult,
    TransientError,
)


def _check_header(value: str, what: str) -> str:
    if "\r" in value or "\n" in value:
        raise AdapterError(f"The {what} contains a line break; the email was not sent.")
    return value


class SmtpAdapter(Adapter):
    slug = "smtp"
    name = "Email (SMTP)"
    kind = AdapterKind.EMAIL
    description = (
        "Sends notifications about manual tasks and the sign-in details of new accounts "
        "through any SMTP server."
    )
    supports_write = False
    supports_account_listing = False
    fields = (
        Field("host", "SMTP server", required=True, placeholder="smtp.example.invalid"),
        Field("port", "Port", kind="int", default=587),
        Field(
            "security",
            "Encryption",
            kind="choice",
            default="starttls",
            choices=(
                ("starttls", "STARTTLS (usually port 587)"),
                ("ssl", "TLS from the start (usually port 465)"),
                ("none", "None (only inside a trusted network)"),
            ),
        ),
        Field("username", "Username"),
        Field("password", "Password", kind="secret"),
        Field("from_address", "From address", kind="email", required=True),
        Field("from_name", "From name", placeholder="IT team"),
        Field("reply_to", "Reply-to address", kind="email"),
    )

    def _connect(self) -> smtplib.SMTP:
        self.require("host", "from_address")
        host = str(self.setting("host"))
        try:
            port = int(self.setting("port", 587))
        except (TypeError, ValueError) as exc:
            raise ConfigurationError("The port must be a number.") from exc
        security = self.setting("security", "starttls")
        timeout = settings.PROVIDER_TIMEOUT_SECONDS
        context = ssl.create_default_context()
        server = None
        try:
            if security == "ssl":
                server = smtplib.SMTP_SSL(host, port, timeout=timeout, context=context)
            else:
                server = smtplib.SMTP(host, port, timeout=timeout)
                if security == "starttls":
                    server.starttls(context=context)
            if self.setting("username"):
                server.login(str(self.setting("username")), str(self.setting("password", "")))
        except Exception as exc:
            if server is not None:
                try:
                    server.close()
                except Exception:  # noqa: BLE001 - closing is best effort
                    pass
            self._reraise(exc, "connect")
        return server

    def _reraise(self, exc: Exception, what: str):
        translated = self._translate(exc, what)
        if translated is exc:
            raise exc
        raise translated from exc

    def _translate(self, exc: Exception, what: str) -> Exception:
        """Map smtplib and socket errors to adapter errors. smtplib's
        exceptions are OSErrors, so the SMTP checks come first."""
        if isinstance(exc, smtplib.SMTPAuthenticationError):
            return ConfigurationError(
                "The SMTP server refused the username or password. Check the connection's "
                "credentials."
            )
        if isinstance(exc, smtplib.SMTPNotSupportedError):
            return ConfigurationError(
                "The SMTP server does not support the chosen encryption or sign-in."
            )
        if isinstance(exc, smtplib.SMTPRecipientsRefused):
            return AdapterError("The SMTP server refused the recipient address.")
        if isinstance(exc, smtplib.SMTPSenderRefused):
            return AdapterError(
                "The SMTP server refused the from address. Check that the server may send for it."
            )
        if isinstance(exc, smtplib.SMTPServerDisconnected | smtplib.SMTPConnectError):
            return TransientError(f"Could not reach the SMTP server ({what}).")
        if isinstance(exc, smtplib.SMTPResponseException):
            if 400 <= exc.smtp_code < 500:
                return TransientError(f"The SMTP server asked to try again later ({what}).")
            return AdapterError(f"The SMTP server rejected the request ({what}).")
        if isinstance(exc, smtplib.SMTPException):
            return AdapterError(f"The SMTP server rejected the request ({what}).")
        if isinstance(exc, ssl.SSLError | ssl.CertificateError):
            return ConfigurationError(
                "The TLS connection to the SMTP server failed. Check the port and encryption."
            )
        if isinstance(exc, OSError):
            # Timeouts, refused connections, DNS failures.
            return TransientError(f"Could not reach the SMTP server ({what}).")
        return exc

    def test_connection(self) -> TestResult:
        server = self._connect()
        try:
            server.noop()
        except Exception as exc:
            self._reraise(exc, "check")
        finally:
            try:
                server.quit()
            except Exception:  # noqa: BLE001 - quitting is best effort
                pass
        return TestResult(True, f"Connected to {self.setting('host')} and signed in.")

    def send(self, *, to: list[str], subject: str, body: str, reply_to: str = "") -> None:
        recipients = [
            _check_header(address.strip(), "recipient") for address in to if address.strip()
        ]
        if not recipients:
            raise AdapterError("The email has no recipient.")
        from_address = _check_header(str(self.setting("from_address", "")), "from address")
        from_name = _check_header(str(self.setting("from_name", "")), "from name")
        message = EmailMessage()
        message["Subject"] = _check_header(subject, "subject")
        message["From"] = formataddr((from_name, from_address)) if from_name else from_address
        message["To"] = ", ".join(recipients)
        reply = reply_to or str(self.setting("reply_to", ""))
        if reply:
            message["Reply-To"] = _check_header(reply, "reply-to address")
        message.set_content(body)
        server = self._connect()
        try:
            server.send_message(message, from_addr=from_address, to_addrs=recipients)
        except Exception as exc:
            self._reraise(exc, "send")
        finally:
            try:
                server.quit()
            except Exception:  # noqa: BLE001 - quitting is best effort
                pass

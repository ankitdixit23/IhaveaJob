"""SMTP email delivery (Gmail with an App Password, or any SMTP provider)."""

from __future__ import annotations

import os
import smtplib
import ssl
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import formataddr, make_msgid
from typing import Protocol


class NotificationError(Exception):
    pass


class Notifier(Protocol):
    channel: str

    def send(self, to: str, subject: str, text: str, html: str | None = None) -> None: ...


@dataclass
class EmailSettings:
    host: str
    port: int
    username: str | None
    password: str | None
    sender: str
    recipients: list[str]
    timeout: float = 30.0

    @classmethod
    def from_env(cls) -> "EmailSettings | None":
        """None when email isn't configured (the monitor still runs)."""
        host = os.getenv("SMTP_HOST")
        recipients = [r.strip() for r in (os.getenv("ALERT_EMAIL_TO") or "").split(",") if r.strip()]
        if not host or not recipients:
            return None
        username = os.getenv("SMTP_USERNAME") or None
        sender = os.getenv("SMTP_FROM") or (formataddr(("IhaveaJob", username)) if username else None)
        if not sender:
            raise NotificationError("Set SMTP_FROM or SMTP_USERNAME so emails have a sender address")
        return cls(host=host, port=int(os.getenv("SMTP_PORT") or 587), username=username,
                   password=os.getenv("SMTP_PASSWORD") or None, sender=sender, recipients=recipients)


class SmtpEmailNotifier:
    channel = "email"

    def __init__(self, settings: EmailSettings):
        self.settings = settings

    def send(self, to: str, subject: str, text: str, html: str | None = None) -> None:
        s = self.settings
        msg = EmailMessage()
        msg["From"], msg["To"], msg["Subject"] = s.sender, to, subject
        msg["Message-ID"] = make_msgid(domain="ihaveajob.local")
        msg.set_content(text)
        if html:
            msg.add_alternative(html, subtype="html")
        context = ssl.create_default_context()
        try:
            if s.port == 465:
                with smtplib.SMTP_SSL(s.host, s.port, timeout=s.timeout, context=context) as smtp:
                    self._login_and_send(smtp, msg)
            else:
                with smtplib.SMTP(s.host, s.port, timeout=s.timeout) as smtp:
                    smtp.starttls(context=context)
                    self._login_and_send(smtp, msg)
        except smtplib.SMTPAuthenticationError as exc:
            raise NotificationError(
                "SMTP login failed. For Gmail use an App Password (Google Account → Security → 2-Step "
                f"Verification → App passwords), not your normal password. Server said: {exc.smtp_code}") from exc
        except (smtplib.SMTPException, OSError) as exc:
            raise NotificationError(f"Could not send email via {s.host}:{s.port}: {exc}") from exc

    def _login_and_send(self, smtp: smtplib.SMTP, msg: EmailMessage) -> None:
        if self.settings.username and self.settings.password:
            smtp.login(self.settings.username, self.settings.password)
        smtp.send_message(msg)

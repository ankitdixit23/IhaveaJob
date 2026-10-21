"""Notification channels. Email (SMTP) is the first; others implement ``Notifier``."""

from .email import EmailSettings, SmtpEmailNotifier, Notifier
from .digest import Digest, build_digest

__all__ = ["Digest", "EmailSettings", "Notifier", "SmtpEmailNotifier", "build_digest"]

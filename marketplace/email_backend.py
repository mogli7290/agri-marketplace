"""A Django email backend that sends through Brevo's REST API.

Why not SMTP? Brevo issues **two different secrets** — an API key and an SMTP
key — and they are not interchangeable. SMTP login with an API key fails with
``535 5.7.8 Authentication failed``, which is a confusing way to discover that
your app is configured.

Talking to ``/v3/smtp/email`` over HTTPS means a single API key works, no extra
dependency (``requests`` is already required), and the same backend on every
platform. If you would rather use plain SMTP, delete this file and point
``DJANGO_EMAIL_BACKEND`` at ``django.core.mail.backends.smtp.EmailBackend`` —
nothing else in the codebase cares which backend is in use.
"""

from __future__ import annotations

import logging
from email.utils import parseaddr

import requests
from django.conf import settings
from django.core.mail.backends.base import BaseEmailBackend

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://api.brevo.com"
SEND_PATH = "/v3/smtp/email"
TIMEOUT = 20


class BrevoEmailBackend(BaseEmailBackend):
    """Send email via Brevo's transactional API."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.base_url = getattr(settings, "BREVO_API_BASE_URL", DEFAULT_BASE_URL).rstrip("/")
        self.api_key = getattr(settings, "BREVO_API_KEY", "")
        if not self.api_key:
            # Allow EMAIL_HOST_PASSWORD to carry the key, so the existing
            # SMTP-shaped env vars still work without a second secret.
            self.api_key = settings.EMAIL_HOST_PASSWORD or ""

    def send_messages(self, email_messages) -> int:
        if not email_messages:
            return 0
        if not self.api_key:
            logger.error("BREVO_API_KEY is not set — no email can be sent.")
            return 0

        sent = 0
        for message in email_messages:
            if not message.recipients():
                continue
            try:
                response = requests.post(
                    f"{self.base_url}{SEND_PATH}",
                    json=self._payload(message),
                    headers={"api-key": self.api_key, "accept": "application/json"},
                    timeout=TIMEOUT,
                )
            except requests.RequestException as exc:
                logger.error("Brevo request failed for %s: %s", message.to, exc)
                if not self.fail_silently:
                    raise
                continue

            if response.status_code >= 400:
                logger.error(
                    "Brevo rejected %s (%s): %s",
                    message.to, response.status_code, response.text[:500],
                )
                if not self.fail_silently:
                    response.raise_for_status()
                continue

            sent += 1
            logger.info("Sent %r to %s via Brevo", message.subject, message.to)

        return sent

    def _payload(self, message) -> dict:
        # DEFAULT_FROM_EMAIL is "Name <addr@example.com>"; Brevo wants the
        # display name and address as separate fields.
        display_name, sender_address = parseaddr(settings.DEFAULT_FROM_EMAIL)
        sender = {"email": sender_address or settings.DEFAULT_FROM_EMAIL}
        if display_name:
            sender["name"] = display_name

        payload = {
            "sender": sender,
            "to": [{"email": to} for to in message.recipients()],
            "subject": message.subject or "",
            "textContent": message.body,
        }

        alternatives = getattr(message, "alternatives", None) or []
        for content, mimetype in alternatives:
            if mimetype == "text/html":
                payload["htmlContent"] = content
                break

        # Headers such as Reply-To, which Brevo carries in its own field.
        if getattr(message, "reply_to", None):
            payload["replyTo"] = {"email": message.reply_to[0]}

        return payload
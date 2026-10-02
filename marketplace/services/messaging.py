"""Outbound email.

A thin wrapper over Django's mail backend so that callers do not care whether
mail is going to a real SMTP relay, the console, or nowhere at all.

The one rule: **sending must never raise into the request.** A farmer whose
transaction email bounces should still get their account created; the failure is
logged (and surfaced loudly in DEBUG) so it can be noticed.
"""

from __future__ import annotations

import logging

from django.conf import settings
from django.core.mail import EmailMultiAlternatives
from django.template.loader import render_to_string

logger = logging.getLogger(__name__)


def send_email(to: str | list[str], subject: str, template: str, context: dict) -> bool:
    """Render ``email/<template>.txt`` (and .html if present) and send it.

    Accepts a single address or a list. Normalising here rather than at each
    call site matters: passing a list to ``EmailMultiAlternatives(to=[to])``
    produces ``[["a@b.com"]]``, which some backends reject and the rest mail
    to a literal string like ``"['a@b.com']"``.

    Returns True when the backend accepted the message. Never raises.
    """
    recipients = [to] if isinstance(to, str) else [address for address in (to or []) if address]
    if not recipients:
        logger.warning("Refusing to send %r: no recipient address", subject)
        return False

    context = {**context, "site_name": settings.SITE_NAME}
    text_body = render_to_string(f"email/{template}.txt", context)
    html_body = ""
    try:
        html_body = render_to_string(f"email/{template}.html", context)
    except Exception:  # noqa: BLE001 - html variant is optional
        html_body = ""

    message = EmailMultiAlternatives(
        subject=subject,
        body=text_body,
        from_email=settings.DEFAULT_FROM_EMAIL,
        to=recipients,
    )
    if html_body:
        message.attach_alternative(html_body, "text/html")

    try:
        sent = message.send(fail_silently=False)
    except Exception as exc:  # noqa: BLE001 - never break the caller
        logger.error("Could not send %r to %s: %s", subject, to, exc)
        if settings.DEBUG:
            logger.warning("Email body was:\n%s", text_body)
        return False

    logger.info("Sent %r to %s", subject, to)
    return bool(sent)
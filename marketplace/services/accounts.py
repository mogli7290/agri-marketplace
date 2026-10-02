"""Account lifecycle: proving a user owns the email address they registered with.

Design:

* The link is a **signed, timestamped token** (Django's ``signing``), and only
  its SHA-256 hash is stored. A leaked database row therefore cannot be used to
  verify an account.
* Issuing a new link replaces the hash, so the previous link stops working.
* Verification is required once, at registration — not on every login. Farmers
  on mobile data should not pay for a code every time.
* Accounts with no ``EmailVerification`` row were created out of band (admin,
  ``createsuperuser``, seed data) and count as verified.
"""

from __future__ import annotations

import hashlib
import logging
import uuid
from urllib.parse import urlparse

from django.conf import settings
from django.core import signing
from django.db import transaction
from django.urls import reverse
from django.utils import timezone
from django.utils.encoding import force_bytes
from django.utils.http import urlsafe_base64_encode

from marketplace.models import EmailVerification
from marketplace.services import messaging

logger = logging.getLogger(__name__)

TOKEN_SALT = "agrimarket.email-verification"


class VerificationError(Exception):
    """Raised when a verification link cannot be honoured."""


def is_verified(user) -> bool:
    """True if the user has proved their address (or was created by an operator)."""
    if user is None:
        return False
    # A OneToOne with no row raises AttributeError, so the default kicks in.
    record = getattr(user, "email_verification", None)
    return record is None or record.is_verified


def verification_required(user) -> bool:
    """Whether this account must be verified before it may sign in."""
    if not settings.EMAIL_VERIFICATION_REQUIRED or user.is_superuser:
        return False
    return getattr(user, "email_verification", None) is not None


def may_sign_in(user) -> bool:
    return not verification_required(user) or is_verified(user)


LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "0.0.0.0", "[::1]", "::1"}


def _is_loopback(url: str) -> bool:
    host = urlparse(url).hostname or ""
    return host.lower() in LOOPBACK_HOSTS


def site_base_url(request=None) -> str:
    """The public base URL, preferring ``SITE_URL`` over the request.

    Behind a proxy (Render, nginx) ``build_absolute_uri`` can hand back ``http://``
    or an internal hostname unless the proxy headers are configured — and a
    reset link that arrives as ``http://`` is a link the browser will warn about
    or silently refuse. ``SITE_URL`` is set explicitly in production, so it wins;
    the request is only a fallback for local development.

    One exception: a loopback ``SITE_URL`` is *not* a public address. A dev who
    browses over the LAN at ``10.0.0.5:8000`` would otherwise mail themselves a
    link to ``127.0.0.1``, which resolves to their phone. When the request proves
    a reachable host exists, trust the request.
    """
    configured = (getattr(settings, "SITE_URL", "") or "").rstrip("/")
    from_request = ""
    if request is not None:
        try:
            from_request = request.build_absolute_uri("/").rstrip("/")
        except Exception:  # noqa: BLE001 - a bad Host must not break a reset email
            from_request = ""

    if configured:
        # A loopback SITE_URL is only trustworthy when the request agrees with it.
        if not (_is_loopback(configured) and from_request and not _is_loopback(from_request)):
            return configured
    return from_request


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def build_token(user) -> str:
    # A nonce makes every link distinct, so re-issuing genuinely supersedes the
    # previous one instead of silently handing out the same string again.
    return signing.dumps(
        {"uid": user.pk, "nonce": uuid.uuid4().hex}, salt=TOKEN_SALT, compress=True
    )


@transaction.atomic
def issue_link(user) -> str:
    """Mint a fresh token, store its hash, and return the token.

    Split out from :func:`issue_verification` so tooling (the
    ``show_verification`` command) can obtain a *valid* link without sending
    mail. A token whose hash was never stored would be rejected at confirm time.
    """
    token = build_token(user)
    record, _ = EmailVerification.objects.select_for_update().get_or_create(user=user)
    record.token_hash = _hash(token)
    record.sent_at = timezone.now()
    record.send_count += 1
    record.save(update_fields=["token_hash", "sent_at", "send_count", "updated_at"])
    return token


@transaction.atomic
def issue_verification(user, base_url: str = "") -> EmailVerification:
    """(Re)issue a verification link for ``user`` and email it.

    Returns the record. Sending failures are logged, not raised — a farmer must
    still be able to reach the resend page.
    """
    token = issue_link(user)
    # Look the record up by user, not by pk: EmailVerification has its own
    # auto id, which only matches the user's id by coincidence.
    record = EmailVerification.objects.get(user=user)
    base = base_url.rstrip("/") or site_base_url()
    url = f"{base}/verify-email/{token}/" if base else ""

    if not url:
        logger.error("No base URL available for the verification link (set SITE_URL)")
        return record

    delivered = messaging.send_email(
        to=user.email,
        subject=f"Confirm your {settings.SITE_NAME} account",
        template="verify_email",
        context={
            "user": user,
            "verification_url": url,
            "expiry_hours": max(1, settings.EMAIL_VERIFICATION_MAX_AGE // 3600),
        },
    )
    if not delivered:
        logger.warning("Verification email to %s was not delivered", user.email)
    return record


def resend_verification(user, base_url: str = "") -> bool:
    """Re-send the link, honouring the cooldown. True if a mail was attempted."""
    if is_verified(user):
        return False
    record = getattr(user, "email_verification", None)
    if record is not None and not record.can_resend():
        return False
    issue_verification(user, base_url=base_url)
    return True


def confirm_token(token: str) -> EmailVerification:
    """Validate a link from the email and mark the address verified.

    Raises ``VerificationError`` for an unknown, tampered, expired or
    already-superseded token.
    """
    max_age = settings.EMAIL_VERIFICATION_MAX_AGE
    try:
        data = signing.loads(token, salt=TOKEN_SALT, max_age=max_age)
        user_id = int(data["uid"])
    except signing.BadSignature as exc:
        raise VerificationError(
            "This link is invalid or has expired. Request a new one below."
        ) from exc
    except (KeyError, TypeError, ValueError) as exc:
        raise VerificationError("This link is invalid. Request a new one below.") from exc

    with transaction.atomic():
        record = (
            EmailVerification.objects.select_for_update()
            .select_related("user")
            .filter(user_id=user_id)
            .first()
        )
        if record is None:
            raise VerificationError("We have no pending verification for that link.")
        if record.token_hash != _hash(token):
            raise VerificationError(
                "This link has already been replaced. Use the most recent email."
            )
        if record.is_verified:
            raise VerificationError("This email address is already verified.")

        record.verified_at = timezone.now()
        record.save(update_fields=["verified_at", "updated_at"])

    logger.info("Email verified for %s", record.user)
    return record

# ---------------------------------------------------------------------------
# Password reset
# ---------------------------------------------------------------------------
def _password_reset_path(user, token: str) -> str:
    """The confirm URL for ``user``, built by ``reverse`` rather than by hand.

    The route carries two segments — ``<uidb64>/<token>`` — and the token
    itself contains a ``.`` and a ``-``. Assembling the string by hand is how
    the link ends up 404ing while every test still passes, because the tests
    reverse the URL themselves instead of reading the one that was emailed.
    """
    return reverse(
        "marketplace:password_reset_confirm",
        kwargs={
            "uidb64": urlsafe_base64_encode(force_bytes(user.pk)),
            "token": token,
        },
    )


def send_password_reset_email(user, token: str, base_url: str = "") -> bool:
    """Email a password-reset link, swallowing every failure.

    Django's built-in reset view raises ``MailError`` when the mail backend
    refuses, which would turn a dead SMTP relay into a 500 on the "forgot
    password" page — precisely when the user is already locked out. The reset
    link matters enough to log loudly and move on.

    Reports whether a message was handed to the backend, so the caller can tell
    the user honestly that nothing was sent.
    """
    base = base_url.rstrip("/") or site_base_url()
    url = f"{base}{_password_reset_path(user, token)}" if base else ""
    if not url:
        logger.error(
            "No base URL for the password reset link to %s (set SITE_URL)", user
        )
        return False

    try:
        return messaging.send_email(
            to=user.email,
            subject=f"Reset your {settings.SITE_NAME} password",
            template="password_reset",
            context={
                "user": user,
                "reset_url": url,
                "site_name": settings.SITE_NAME,
                "expiry_hours": max(1, settings.PASSWORD_RESET_TIMEOUT // 3600),
            },
        )
    except Exception as exc:  # noqa: BLE001 - a locked-out user still needs a page
        logger.error("Could not build the password reset email for %s: %s", user, exc)
        return False

"""Razorpay payment integration (UPI, cards, netbanking).

Implemented against Razorpay's REST API with ``requests`` rather than the
official SDK, so there is no extra dependency and the payment flow is easy to
audit. All cryptographic checks use Razorpay's documented HMAC-SHA256 scheme.

This module owns the whole gateway conversation — creating provider orders,
verifying callbacks and webhooks, recording captures and issuing refunds — so
the web views, the API and the webhook all behave identically. The invariant
every path upholds is: **a payment only ever settles the order it was created
for**, which is why ``record_capture`` matches on ``(order, provider_order_id)``
rather than on the provider id alone.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
from decimal import Decimal

import requests
from django.conf import settings
from django.db import transaction

from marketplace.models import Payment
from marketplace.services import orders as order_service

logger = logging.getLogger(__name__)

RAZORPAY_API_BASE = "https://api.razorpay.com/v1"
REQUEST_TIMEOUT = 15

#: Razorpay refuses amounts below one rupee.
MIN_PAYABLE_PAISE = 100

#: Webhook events that mean "the money is in".
CAPTURED_EVENTS = frozenset({"payment.captured", "order.paid"})

# One session for the whole process so keep-alive connections are reused.
_session = requests.Session()


class PaymentError(Exception):
    """Raised when a payment cannot be started, verified or settled."""


def is_configured() -> bool:
    return bool(settings.RAZORPAY_KEY_ID and settings.RAZORPAY_KEY_SECRET)


def _auth() -> tuple[str, str]:
    return settings.RAZORPAY_KEY_ID, settings.RAZORPAY_KEY_SECRET


def to_paise(amount: Decimal) -> int:
    """Convert rupees to the smallest currency unit (paise for INR)."""
    # str() avoids Decimal(float)'s binary expansion, which can shift the
    # rounded paise figure on values like 2.675.
    return int((Decimal(str(amount)) * 100).to_integral_value())


def _provider_error(response) -> str:
    """Pull the provider's own error message out of the response body."""
    try:
        body = response.json().get("error", {})
        description = body.get("description") or body.get("reason")
        if description:
            return str(description)[:255]
    except ValueError:
        pass
    return f"Provider responded with HTTP {response.status_code}."


def _request(method: str, path: str, *, payload: dict | None = None,
             idempotency_key: str = "") -> dict:
    """Call the provider, turning transport and HTTP failures into PaymentError."""
    headers = {"Idempotency-Key": idempotency_key} if idempotency_key else {}
    try:
        response = _session.request(
            method,
            f"{RAZORPAY_API_BASE}{path}",
            json=payload,
            auth=_auth(),
            headers=headers,
            timeout=REQUEST_TIMEOUT,
        )
    except requests.RequestException as exc:
        logger.error("Razorpay %s %s failed: %s", method, path, exc)
        raise PaymentError("Could not reach the payment provider.") from exc

    if response.status_code >= 400:
        logger.error(
            "Razorpay %s %s -> %s: %s", method, path, response.status_code, response.text[:500]
        )
        raise PaymentError(_provider_error(response))

    try:
        return response.json()
    except ValueError as exc:
        raise PaymentError("The payment provider sent an unreadable response.") from exc


# ---------------------------------------------------------------------------
# Gateway calls
# ---------------------------------------------------------------------------
def create_payment_order(order, idempotency_key: str | None = None) -> dict:
    """Create a Razorpay order for a marketplace ``Order``.

    The amount is always derived server-side from the order, never from the
    request, so a tampered client cannot change what is owed.
    """
    if not is_configured():
        raise PaymentError("Online payments are not configured")

    amount_paise = to_paise(order.total_amount)
    if amount_paise < MIN_PAYABLE_PAISE:
        raise PaymentError("Order total is below the minimum payable amount of ₹1.00.")

    payload = {
        "amount": amount_paise,
        "currency": settings.RAZORPAY_CURRENCY,
        "receipt": f"order_{order.pk}",
        "notes": {"marketplace_order_id": str(order.pk)},
    }
    # The key is derived from the order *and* the amount, so a double-clicked
    # button or a retried request reuses the provider order instead of creating
    # a second charge, while a genuinely changed total still gets a fresh one.
    key = idempotency_key or f"agm-order-{order.pk}-{amount_paise}"
    return _request("POST", "/orders", payload=payload, idempotency_key=key)


def fetch_payment(payment_id: str) -> dict:
    """Read a payment back from the provider (used for reconciliation)."""
    if not payment_id:
        raise PaymentError("A provider payment id is required.")
    return _request("GET", f"/payments/{payment_id}")


def create_refund(payment_id: str, amount: Decimal | None = None) -> dict:
    """Refund a captured payment in full, or partially when ``amount`` is given."""
    payload: dict = {"notes": {"source": "agrimarket"}}
    if amount is not None:
        refund_paise = to_paise(amount)
        if refund_paise < 1:
            raise PaymentError("Refund amount must be greater than zero.")
        payload["amount"] = refund_paise
    return _request("POST", f"/payments/{payment_id}/refund", payload=payload)


# ---------------------------------------------------------------------------
# Signature verification
# ---------------------------------------------------------------------------
def verify_checkout_signature(provider_order_id: str, payment_id: str, signature: str) -> bool:
    """Verify the signature returned by Razorpay Checkout after a successful payment."""
    if not is_configured() or not provider_order_id or not payment_id:
        return False
    body = f"{provider_order_id}|{payment_id}".encode()
    expected = hmac.new(
        settings.RAZORPAY_KEY_SECRET.encode(), body, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, signature or "")


def verify_webhook_signature(raw_body: bytes, signature: str) -> bool:
    """Verify a webhook payload using the configured webhook secret."""
    secret = settings.RAZORPAY_WEBHOOK_SECRET
    if not secret:
        return False
    expected = hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature or "")


# ---------------------------------------------------------------------------
# Settlement
# ---------------------------------------------------------------------------
def ensure_payable(order) -> None:
    """Raise unless the order may still take a payment."""
    try:
        order_service.ensure_payable(order)
    except order_service.OrderError as exc:
        raise PaymentError(str(exc)) from exc


def start_payment(order) -> Payment:
    """Return an in-flight gateway payment for ``order``, creating one if needed.

    Re-clicking "Pay" reuses the open provider order, so the buyer never ends
    up with two live charges for the same basket.
    """
    ensure_payable(order)

    reusable = (
        Payment.objects.filter(
            order=order, provider="razorpay", status="created", provider_order_id__gt=""
        )
        .order_by("-created_at")
        .first()
    )
    if reusable is not None and reusable.amount == order.total_amount:
        return reusable

    provider_order = create_payment_order(order)
    return Payment.objects.create(
        order=order,
        provider_order_id=provider_order["id"],
        amount=order.total_amount,
        currency=provider_order.get("currency", settings.RAZORPAY_CURRENCY),
        status="created",
    )


def _mark_payment_failed(payment: Payment, reason: str) -> None:
    """Record a failure in its own transaction.

    Deliberately *not* inside the settlement transaction: raising afterwards
    would roll the update back and lose the evidence of a bad signature.
    """
    with transaction.atomic():
        Payment.objects.filter(pk=payment.pk).update(
            status="failed", failure_reason=reason[:255]
        )


def record_capture(order, *, provider_order_id: str, payment_id: str, signature: str) -> Payment:
    """Settle an order from a verified checkout callback.

    Both the signature *and* the provider order id are checked against a payment
    that belongs to this order. Verifying the signature alone is not enough: a
    valid signature for a ₹10 order would otherwise be replayable to settle a
    different order.
    """
    ensure_payable(order)

    payment = (
        Payment.objects.select_related("order")
        .filter(order=order, provider="razorpay", provider_order_id=provider_order_id)
        .first()
    )
    if payment is None:
        logger.warning(
            "Checkout callback for order %s referenced unknown provider order %r",
            order.pk, provider_order_id,
        )
        raise PaymentError("We could not match this payment to the order.")

    if not verify_checkout_signature(provider_order_id, payment_id, signature):
        _mark_payment_failed(payment, "Checkout signature mismatch")
        raise PaymentError("Payment verification failed.")

    with transaction.atomic():
        locked = Payment.objects.select_for_update().get(pk=payment.pk)
        if locked.status == "captured":
            # Duplicate callback (retry, double submit) — already settled.
            return locked
        # Re-check under the lock: the order may have been cancelled or settled
        # while we were verifying the signature.
        try:
            order_service.ensure_payable(locked.order)
        except order_service.OrderError as exc:
            raise PaymentError(str(exc)) from exc
        locked.status = "captured"
        locked.provider_payment_id = payment_id or locked.provider_payment_id
        locked.signature = signature[:255]
        locked.failure_reason = ""
        locked.save(
            update_fields=[
                "status", "provider_payment_id", "signature", "failure_reason", "updated_at",
            ]
        )
        order_service.mark_order_paid(locked.order, payment_id=locked.provider_payment_id)
        return locked


@transaction.atomic
def handle_webhook_event(event: dict) -> str:
    """Apply a Razorpay webhook event. Returns a short outcome string.

    Idempotent by design: Razorpay retries until it gets a 2xx, so every event
    may arrive several times.
    """
    event_type = event.get("event", "")
    payload = event.get("payload") or {}
    entity = (payload.get("payment") or {}).get("entity") or {}
    if not entity:
        # "order.paid" and friends carry the payment under payload.payment too,
        # but fall back to the order entity if a future event does not.
        entity = (payload.get("order") or {}).get("entity") or {}

    # Guard against a blank id matching payments that never went through the
    # gateway (direct UPI claims leave provider_order_id empty).
    provider_order_id = entity.get("order_id") or ""
    if not provider_order_id:
        return "ignored"

    payment = (
        Payment.objects.select_for_update()
        .select_related("order")
        .filter(provider="razorpay", provider_order_id=provider_order_id)
        .first()
    )
    if payment is None:
        logger.info("Webhook %s for unknown provider order %s", event_type, provider_order_id)
        return "ignored"

    if event_type in CAPTURED_EVENTS:
        if payment.status == "refunded":
            return "duplicate"
        already_paid = payment.order.payment_status == "paid"
        payment.status = "captured"
        payment.provider_payment_id = entity.get("id") or payment.provider_payment_id
        payment.method = (entity.get("method") or payment.method)[:40]
        payment.failure_reason = ""
        payment.save(
            update_fields=[
                "status", "provider_payment_id", "method", "failure_reason", "updated_at",
            ]
        )
        if not already_paid:
            order_service.mark_order_paid(
                payment.order,
                payment_id=payment.provider_payment_id,
                note=f"Confirmed by Razorpay ({event_type})",
            )
        return "captured"

    if event_type == "payment.authorized":
        if payment.status not in {"captured", "refunded"}:
            payment.status = "authorized"
            payment.provider_payment_id = entity.get("id") or payment.provider_payment_id
            payment.method = (entity.get("method") or payment.method)[:40]
            payment.save(
                update_fields=["status", "provider_payment_id", "method", "updated_at"]
            )
        return "authorized"

    if event_type == "payment.failed":
        payment.status = "failed"
        payment.failure_reason = (entity.get("error_description") or "Payment failed")[:255]
        payment.save(update_fields=["status", "failure_reason", "updated_at"])
        order = payment.order
        if order.payment_status == "pending":
            order.payment_status = "failed"
            order.save(update_fields=["payment_status", "updated_at"])
        return "failed"

    if event_type == "refund.processed":
        payment.status = "refunded"
        payment.save(update_fields=["status", "updated_at"])
        order_service.mark_order_refunded(payment.order, note="Refund processed by Razorpay")
        return "refunded"

    return "ignored"


@transaction.atomic
def refund_order_payment(order, amount: Decimal | None = None, note: str = "") -> Payment:
    """Refund the latest captured gateway payment and flip the order to refunded.

    Raises ``PaymentError`` when there is nothing refundable, so a double click
    on the admin action cannot produce two refunds.
    """
    payment = (
        Payment.objects.select_for_update()
        .filter(order=order, provider="razorpay", status="captured")
        .order_by("-created_at")
        .first()
    )
    if payment is None or not payment.provider_payment_id:
        raise PaymentError("There is no captured online payment to refund for this order.")

    # Raises PaymentError, which rolls the block back: the order stays "paid"
    # rather than being marked refunded for money that never came back.
    create_refund(payment.provider_payment_id, amount)

    payment.status = "refunded"
    payment.save(update_fields=["status", "updated_at"])
    order_service.mark_order_refunded(order, note=note or f"Refund issued for {payment.provider_payment_id}")
    return payment
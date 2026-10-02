"""Direct UPI payments — no gateway, no API keys.

Instead of routing money through a provider like Razorpay, the buyer pays
straight to a UPI ID (VPA) configured in settings, using their own UPI app
(GPay, PhonePe, Paytm, …). The app builds the standard NPCDL deep link
(``upi://pay?pa=…&am=…``), which is also what a UPI QR code encodes, so one
string serves both the "open UPI app" button and the scannable QR.

The money lands in the configured UPI account. The buyer's claim (with UTR)
is recorded as a ``Payment`` in ``authorized`` state; staff verify the credit
in their UPI app and confirm it in the admin, which settles the order.

Because there is no gateway to ask, the two things worth defending here are:
the configured VPA (a typo means payments to nobody) and duplicate/replayed
claims (nothing stops a buyer pressing the button twice).
"""

from __future__ import annotations

import re
from decimal import Decimal
from urllib.parse import quote_plus

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from marketplace.models import Payment
from marketplace.services import orders as order_service
from marketplace.services import payments

UPI_CURRENCY = "INR"

#: Money that goes to the *platform*, settled by the marketplace.
PLATFORM_PROVIDER = "upi_direct"
#: Money that goes straight to the *farmer's* own UPI ID. The buyer pays, but
#: the farmer is the one who sees the credit — so the **seller** confirms
#: receipt, not the buyer. The platform is not a party to this payment.
FARMER_PROVIDER = "upi_to_farmer"

#: A VPA looks like ``localpart@bankhandle``, e.g. ``agrimarket@okaxis``.
VPA_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{1,63}@[A-Za-z][A-Za-z0-9]{1,31}$")

#: UTRs are alphanumeric references issued by the UPI app (12-24 digits in
#: practice). Anything outside this range is a typo or a fabrication.
UTR_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9\-_/]{5,63}$")


class UpiError(Exception):
    """Raised when a direct-UPI claim cannot be recorded."""


def is_configured() -> bool:
    """Direct UPI is available when a syntactically valid UPI ID (VPA) is set."""
    return bool(settings.UPI_ID)


def is_valid_vpa(value: str) -> bool:
    return bool(VPA_RE.match((value or "").strip()))


def is_plausible_utr(value: str) -> bool:
    return bool(UTR_RE.match((value or "").strip()))


def payee_name() -> str:
    """Display name shown inside the buyer's UPI app."""
    return settings.UPI_PAYEE_NAME or settings.SITE_NAME


def build_upi_link(order) -> str:
    """Build the NPCDL ``upi://pay`` deep link for an order's exact amount.

    The amount is always derived server-side from the order; the client never
    supplies it, so a tampered link can't change what is owed.
    """
    if not is_configured():
        raise ValueError("Direct UPI is not configured (set UPI_ID)")

    amount = order.total_amount.quantize(Decimal("0.01"))
    return (
        "upi://pay"
        f"?pa={quote_plus(settings.UPI_ID)}"
        f"&pn={quote_plus(payee_name())}"
        f"&am={amount}"
        f"&cu={UPI_CURRENCY}"
        f"&tr=order_{order.pk}"
        f"&tn={quote_plus(f'AgriMarket order #{order.pk}')}")


@transaction.atomic
def record_claim(order, utr: str) -> Payment:
    """Record that the buyer says they paid, and return the pending ``Payment``.

    This only records the *claim*. Settlement is deliberately not done here:
    staff verify the credit in their UPI app and confirm it in the admin, so a
    false "I paid" cannot mark an order paid on its own.
    """
    try:
        order_service.ensure_payable(order)
    except order_service.OrderError as exc:
        raise UpiError(str(exc)) from exc

    reference = (utr or "").strip()
    if not reference:
        raise UpiError("Enter the transaction reference (UTR) shown in your UPI app.")
    if not is_plausible_utr(reference):
        raise UpiError("That does not look like a UPI transaction reference (UTR).")
    if Payment.objects.filter(
        provider="upi_direct", provider_payment_id=reference
    ).exclude(order=order).exists():
        raise UpiError("That reference has already been recorded against another order.")

    if Payment.objects.filter(
        order=order, provider="upi_direct", status="authorized"
    ).exists():
        raise UpiError("A payment for this order is already awaiting confirmation.")

    return Payment.objects.create(
        order=order,
        provider="upi_direct",
        provider_payment_id=reference,
        amount=order.total_amount,
        currency=UPI_CURRENCY,
        status="authorized",
        method="upi",
    )


def build_seller_upi_link(order, farmer_upi_id: str, payee_name: str = "") -> str:
    """Deep link that pays the *farmer* directly for an exact order amount.

    Direct transfers carry no platform fee, so the amount owed is the order
    total — which for a direct order is simply the subtotal.
    """
    if not is_valid_vpa(farmer_upi_id):
        raise ValueError("The farmer has not set up a valid UPI ID")

    amount = order.total_amount.quantize(Decimal("0.01"))
    name = payee_name or order.listing.farmer.full_name
    return (
        "upi://pay"
        f"?pa={quote_plus(farmer_upi_id)}"
        f"&pn={quote_plus(name)}"
        f"&am={amount}"
        f"&cu={UPI_CURRENCY}"
        f"&tr=order_{order.pk}"
        f"&tn={quote_plus(f'Direct order #{order.pk}')}")


@transaction.atomic
def record_seller_payment(order, utr: str) -> Payment:
    """Record a buyer's claim that they paid the farmer directly.

    The claim is stored but nothing is settled: the farmer sees the credit in
    their own UPI app and confirms it (see :func:`confirm_seller_receipt`).
    """
    try:
        payments.ensure_payable(order)
    except payments.PaymentError as exc:
        raise UpiError(str(exc)) from exc

    if order.payment_route != "direct":
        raise UpiError("This order is not set up for direct payment to the farmer.")

    reference = (utr or "").strip()
    if not reference:
        raise UpiError("Enter the transaction reference (UTR) shown in your UPI app.")
    if not is_plausible_utr(reference):
        raise UpiError("That does not look like a UPI transaction reference (UTR).")
    if Payment.objects.filter(
        provider=FARMER_PROVIDER, provider_payment_id=reference
    ).exclude(order=order).exists():
        raise UpiError("That reference has already been recorded against another order.")
    if Payment.objects.filter(
        order=order, provider=FARMER_PROVIDER, status="authorized"
    ).exists():
        raise UpiError("A payment for this order is already awaiting confirmation.")

    return Payment.objects.create(
        order=order,
        provider=FARMER_PROVIDER,
        provider_payment_id=reference,
        amount=order.total_amount,
        currency=UPI_CURRENCY,
        status="authorized",
        method="upi",
    )


@transaction.atomic
def confirm_seller_receipt(payment: Payment, user) -> Payment:
    """The farmer confirms the money landed. Settles the order."""
    order = Payment.objects.select_for_update().get(pk=payment.pk)

    if order.provider != FARMER_PROVIDER:
        raise UpiError("This is not a direct payment to a farmer.")
    if order.status != "authorized":
        raise UpiError("This payment is not awaiting confirmation.")

    farmer = getattr(user, "farmer_profile", None)
    if farmer is None or order.order.listing.farmer_id != farmer.id:
        raise UpiError("Only the farmer who sold this can confirm receipt.")

    locked = Payment.objects.select_for_update().get(pk=payment.pk)
    locked.status = "captured"
    locked.reconciled_at = timezone.now()
    locked.save(update_fields=["status", "reconciled_at", "updated_at"])
    order_service.mark_order_paid(
        locked.order,
        payment_id=locked.provider_payment_id,
        note="Farmer confirmed the direct UPI credit",
    )
    return locked


@transaction.atomic
def confirm_claim(payment: Payment, note: str = "UPI payment confirmed by staff",
                   reconciliation=None) -> Payment:
    """Settle an order once the credit has been verified.

    Either by hand (staff checked their UPI app) or automatically from a bank
    statement matched by :mod:`marketplace.services.reconciliation`.
    """
    if payment.provider != "upi_direct":
        raise UpiError("Only direct-UPI payments can be confirmed this way.")
    if payment.status != "authorized":
        raise UpiError("This payment is not awaiting confirmation.")

    locked = Payment.objects.select_for_update().get(pk=payment.pk)
    locked.status = "captured"
    locked.reconciled_at = timezone.now()
    if reconciliation is not None:
        locked.reconciliation = reconciliation
    locked.save(update_fields=["status", "reconciled_at", "reconciliation", "updated_at"])
    order_service.mark_order_paid(
        locked.order, payment_id=locked.provider_payment_id, note=note
    )
    return locked


@transaction.atomic
def reverse_claim(payment: Payment, note: str = "UPI payment reversed by staff") -> Payment:
    """Record that money was sent back manually (there is no gateway to call)."""
    if payment.provider != "upi_direct":
        raise UpiError("Only direct-UPI payments can be reversed here.")
    if payment.status == "refunded":
        raise UpiError("This payment has already been reversed.")
    if payment.status != "captured":
        raise UpiError("Only captured payments can be reversed.")

    locked = Payment.objects.select_for_update().get(pk=payment.pk)
    locked.status = "refunded"
    locked.failure_reason = (note or "")[:255]
    locked.save(update_fields=["status", "failure_reason", "updated_at"])
    order_service.mark_order_refunded(locked.order, note=note)
    return locked
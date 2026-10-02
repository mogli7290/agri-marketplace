"""Settlement statements — turning collected money into money owed to a person.

Two kinds of statement come out of this module:

* **Farmer payouts** for platform-collected orders. A direct transfer put the
  money in the farmer's own UPI account and the platform never handled it, so
  there is nothing to settle — including those would be paying twice.
* **Delivery-partner payouts** for completed shipments. ``Order.delivery_fee``
  is money the buyer handed over for transport; it belongs to whoever drove the
  truck, not to the farmer.

The rules, in order of importance:

* **Exactly one recipient per statement**, enforced by a check constraint.
* **An order or shipment can appear in exactly one payout.** Enforced in the
  database by ``OneToOneField`` on ``PayoutItem``, not by application logic — a
  race between two settlement runs cannot pay anyone twice.
* **Money is never moved by this module.** It produces the statement; a human
  transfers the money and records the reference. Integrating a real payout API
  (RazorpayX, Cashfree Payouts) means replacing :func:`mark_paid` only.
"""

from __future__ import annotations

import logging
from datetime import date, timedelta
from decimal import Decimal, ROUND_HALF_UP

from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import Count, Sum
from django.utils import timezone

from marketplace.models import (
    DeliveryPartner,
    FarmerProfile,
    Listing,
    Order,
    Payout,
    PayoutItem,
    PayoutMethod,
    Shipment,
)
from marketplace.services import deals as deals_service
from marketplace.services import notifications

logger = logging.getLogger(__name__)


class PayoutError(Exception):
    """Raised when a payout cannot be built or moved to the requested state."""


def _q(value) -> Decimal:
    return Decimal(value).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def hold_cutoff(reference: date | None = None) -> date:
    """Earliest order date that may be settled.

    Orders sit in a hold window before they become payable, so a return, a
    quality claim or a chargeback can still be taken back before the money has
    left. Set ``PAYOUT_HOLD_DAYS=0`` to settle immediately.
    """
    days = int(getattr(settings, "PAYOUT_HOLD_DAYS", 7))
    reference = reference or timezone.localdate()
    return reference - timedelta(days=days)


def eligible_orders(farmer: FarmerProfile, *, until: date | None = None) -> list[Order]:
    """Paid, platform-collected orders for this farmer that are ready to settle.

    Excluded: direct orders (already in the farmer's account), unpaid orders,
    and anything inside the hold window or already claimed by a payout.
    """
    cutoff = hold_cutoff(until)
    return list(
        Order.objects.filter(
            listing__farmer=farmer,
            payment_route="platform",
            payment_status="paid",
        )
        .filter(status__in=["paid", "delivered"])
        .exclude(created_at__date__gt=cutoff)
        .exclude(payout_item__isnull=False)
        .order_by("created_at")
    )


def pending_summary(farmer: FarmerProfile, *, until: date | None = None) -> dict:
    """What the farmer is owed right now, and what the platform retained."""
    orders = eligible_orders(farmer, until=until)
    gross = sum((o.farmer_payout for o in orders), Decimal("0.00"))
    commission = sum((o.platform_fee for o in orders), Decimal("0.00"))
    delivery = sum((o.delivery_fee for o in orders), Decimal("0.00"))
    return {
        "orders": orders,
        "order_count": len(orders),
        "gross_amount": _q(gross),
        "commission_amount": _q(commission),
        "delivery_amount": _q(delivery),
    }


def farmer_totals(farmer: FarmerProfile) -> dict:
    """Lifetime earnings, split by what was paid out and what is still owed."""
    items = PayoutItem.objects.filter(payout__farmer=farmer)
    paid = items.filter(payout__status="paid").aggregate(
        total=Sum("amount"), count=Count("id")
    )
    outstanding = items.exclude(payout__status__in=["paid", "cancelled"]).aggregate(
        total=Sum("amount")
    )

    return {
        "paid_total": _q(paid["total"] or Decimal("0")),
        "paid_orders": paid["count"] or 0,
        "outstanding_total": _q(outstanding["total"] or Decimal("0")),
    }


@transaction.atomic
def build_statement(farmer: FarmerProfile, *, period_start: date, period_end: date,
                    adjustments: Decimal = Decimal("0"), notes: str = "") -> Payout:
    """Create a draft statement covering every eligible order in the period."""
    if period_end < period_start:
        raise PayoutError("The period ends before it starts.")
    if period_end > timezone.localdate():
        raise PayoutError("You cannot settle a period that has not finished.")

    orders = [
        order
        for order in eligible_orders(farmer, until=period_end)
        if period_start <= order.created_at.date() <= period_end
    ]
    if not orders:
        raise PayoutError("No settled orders are waiting for this farmer in that period.")

    method = deals_service.payout_method_for(farmer)
    payout = Payout.objects.create(
        farmer=farmer,
        period_start=period_start,
        period_end=period_end,
        order_count=len(orders),
        gross_amount=_q(sum((o.farmer_payout for o in orders), Decimal("0"))),
        commission_amount=_q(sum((o.platform_fee for o in orders), Decimal("0"))),
        delivery_amount=_q(sum((o.delivery_fee for o in orders), Decimal("0"))),
        adjustments=_q(adjustments),
        method=method.kind if method else "",
    )
    payout.net_amount = _q(payout.gross_amount + payout.adjustments)
    payout.notes = (notes or "")[:2000]
    payout.save(update_fields=["net_amount", "notes"])

    # The OneToOne on PayoutItem.order is the real guard against paying twice.
    # The savepoint lets us turn a lost race into a clear error.
    try:
        with transaction.atomic():
            PayoutItem.objects.bulk_create(
                [
                    PayoutItem(
                        payout=payout,
                        order=order,
                        amount=_q(order.farmer_payout),
                        commission=_q(order.platform_fee),
                        delivery_fee=_q(order.delivery_fee),
                    )
                    for order in orders
                ]
            )
    except IntegrityError as exc:
        raise PayoutError(
            "Another settlement claimed one of these orders first. Re-run the period."
        ) from exc

    logger.info(
        "Payout %s drafted for %s: %s orders, ₹%s",
        payout.pk, farmer, len(orders), payout.net_amount,
    )
    return payout


# ---------------------------------------------------------------------------
# Delivery partners
# ---------------------------------------------------------------------------
def eligible_shipments(partner: DeliveryPartner, *, until: date | None = None) -> list[Shipment]:
    """Completed shipments for this partner that are ready to be paid.

    A shipment is money the *buyer* paid for delivery. It never belonged to the
    farmer, so it is settled on its own statement against
    ``Shipment.planned_cost``.

    A shipment carrying a **disputed** proof is excluded: the buyer says the
    produce never arrived, so the charge is held until staff resolve it. Paying
    it and chasing afterwards would leave the transporter chasing the buyer for
    money the platform had already committed to.
    """
    cutoff = hold_cutoff(until)
    return list(
        Shipment.objects.filter(partner=partner, status="completed")
        .exclude(created_at__date__gt=cutoff)
        .exclude(payout_item__isnull=False)
        .exclude(proofs__status="disputed")
        .order_by("created_at")
    )


def pending_partner_summary(partner: DeliveryPartner, *, until: date | None = None) -> dict:
    """What the transporter has earned so far and is owed right now."""
    shipments = eligible_shipments(partner, until=until)
    owed = sum((s.planned_cost for s in shipments), Decimal("0.00"))
    return {
        "shipments": shipments,
        "shipment_count": len(shipments),
        "gross_amount": _q(owed),
        "distance_km": _q(
            sum((s.planned_distance_km for s in shipments), Decimal("0.00"))
        ),
    }


def partner_totals(partner: DeliveryPartner) -> dict:
    """Lifetime transporter earnings, split by paid and still owed."""
    items = PayoutItem.objects.filter(payout__delivery_partner=partner)
    paid = items.filter(payout__status="paid").aggregate(
        total=Sum("amount"), count=Count("id")
    )
    outstanding = items.exclude(payout__status__in=["paid", "cancelled"]).aggregate(
        total=Sum("amount")
    )
    return {
        "paid_total": _q(paid["total"] or Decimal("0")),
        "paid_shipments": paid["count"] or 0,
        "outstanding_total": _q(outstanding["total"] or Decimal("0")),
    }


def payout_method_for_partner(partner: DeliveryPartner) -> PayoutMethod | None:
    return PayoutMethod.objects.filter(delivery_partner=partner, is_primary=True).first()


@transaction.atomic
def build_partner_statement(partner: DeliveryPartner, *, period_start: date, period_end: date,
                            adjustments: Decimal = Decimal("0"), notes: str = "") -> Payout:
    """Draft a statement paying a transporter for the shipments they completed."""
    if period_end < period_start:
        raise PayoutError("The period ends before it starts.")
    if period_end > timezone.localdate():
        raise PayoutError("You cannot settle a period that has not finished.")

    shipments = [
        shipment
        for shipment in eligible_shipments(partner, until=period_end)
        if period_start <= shipment.created_at.date() <= period_end
    ]
    if not shipments:
        raise PayoutError("No completed shipments are waiting for this partner in that period.")

    method = payout_method_for_partner(partner)
    gross = _q(sum((s.planned_cost for s in shipments), Decimal("0")))
    payout = Payout.objects.create(
        delivery_partner=partner,
        period_start=period_start,
        period_end=period_end,
        order_count=len(shipments),
        gross_amount=gross,
        # Every rupee here is delivery money, so gross and delivery agree.
        delivery_amount=gross,
        adjustments=_q(adjustments),
        method=method.kind if method else "",
    )
    payout.net_amount = _q(gross + payout.adjustments)
    payout.notes = (notes or "")[:2000]
    payout.save(update_fields=["net_amount", "notes"])

    try:
        with transaction.atomic():
            PayoutItem.objects.bulk_create(
                [
                    PayoutItem(
                        payout=payout,
                        shipment=shipment,
                        amount=_q(shipment.planned_cost),
                        delivery_fee=_q(shipment.planned_cost),
                    )
                    for shipment in shipments
                ]
            )
    except IntegrityError as exc:
        raise PayoutError(
            "Another settlement claimed one of these shipments first. Re-run the period."
        ) from exc

    logger.info(
        "Payout %s drafted for partner %s: %s shipments, ₹%s",
        payout.pk, partner, len(shipments), payout.net_amount,
    )
    return payout


def build_all_partner_statements(*, period_start: date, period_end: date,
                                 adjustments: Decimal = Decimal("0"),
                                 notes: str = "") -> dict:
    """Draft one statement per partner with completed shipments in the period."""
    created: list[Payout] = []
    skipped: dict[str, str] = {}

    for partner in DeliveryPartner.objects.order_by("pk"):
        try:
            created.append(
                build_partner_statement(
                    partner,
                    period_start=period_start,
                    period_end=period_end,
                    adjustments=adjustments,
                    notes=notes,
                )
            )
        except PayoutError as exc:
            skipped[str(partner)] = str(exc)

    return {
        "created": created,
        "skipped": skipped,
        "total_amount": _q(sum((p.net_amount for p in created), Decimal("0"))),
    }


def build_all_statements(*, period_start: date, period_end: date,
                         adjustments: Decimal = Decimal("0"),
                         notes: str = "") -> dict:
    """Draft one statement per farmer with money waiting in the period.

    The weekly settlement run. Farmers with nothing to settle are skipped
    rather than producing empty statements.
    """
    created: list[Payout] = []
    skipped: dict[str, str] = {}

    farmers = FarmerProfile.objects.filter(
        pk__in=Listing.objects.values("farmer_id")
    ).order_by("pk")

    for farmer in farmers:
        try:
            created.append(
                build_statement(
                    farmer,
                    period_start=period_start,
                    period_end=period_end,
                    adjustments=adjustments,
                    notes=notes,
                )
            )
        except PayoutError as exc:
            skipped[str(farmer)] = str(exc)

    return {
        "created": created,
        "skipped": skipped,
        "total_amount": _q(sum((p.net_amount for p in created), Decimal("0"))),
    }


def approve(payout: Payout, user=None) -> Payout:
    """Approve a draft for payment. After this the amounts are frozen."""
    payout = (
        Payout.objects.select_for_update()
        .select_related("farmer", "delivery_partner")
        .get(pk=payout.pk)
    )
    if payout.status != "draft":
        raise PayoutError(f"Only draft payouts can be approved (this one is {payout.status}).")
    if payout.net_amount < 0:
        raise PayoutError("A negative payout cannot be approved; use a refund instead.")

    payout.status = "approved"
    payout.approved_at = timezone.now()
    payout.processed_by = user
    payout.save(update_fields=["status", "approved_at", "processed_by", "updated_at"])
    logger.info("Payout %s approved (₹%s)", payout.pk, payout.net_amount)
    return payout


def mark_paid(payout: Payout, reference: str, user=None) -> Payout:
    """Record that the money was transferred.

    ``reference`` is the bank's or gateway's payout id — the thing that makes
    this statement auditable rather than just a number in a table.
    """
    payout = Payout.objects.select_for_update().get(pk=payout.pk)
    if payout.status != "approved":
        raise PayoutError(f"Only approved payouts can be marked paid (this one is {payout.status}).")
    if not (reference or "").strip():
        raise PayoutError("Enter the bank or gateway payout reference.")

    now = timezone.now()
    payout.status = "paid"
    payout.reference = reference.strip()[:100]
    payout.paid_at = now
    payout.processed_by = user
    payout.save(update_fields=["status", "reference", "paid_at", "processed_by", "updated_at"])

    PayoutItem.objects.filter(payout=payout).update(settled_at=now)
    logger.info("Payout %s paid (₹%s, ref %s)", payout.pk, payout.net_amount, payout.reference)
    notifications.payout_settled(payout)
    return payout


def cancel(payout: Payout, user=None, reason: str = "") -> Payout:
    """Abandon a payout and release its orders back into the next run."""
    payout = Payout.objects.select_for_update().get(pk=payout.pk)
    if payout.status == "paid":
        raise PayoutError("A paid payout cannot be cancelled.")
    if payout.status == "cancelled":
        raise PayoutError("This payout is already cancelled.")

    payout.items.all().delete()  # its orders/shipments become eligible again
    payout.status = "cancelled"
    payout.processed_by = user
    if reason:
        payout.notes = (payout.notes + f"\nCancelled: {reason}").strip()[:2000]
    payout.save(update_fields=["status", "processed_by", "notes", "updated_at"])

    logger.info("Payout %s cancelled, its items released", payout.pk)
    return payout


def statement_for(payout: Payout) -> dict:
    """Plain-data summary for a statement PDF or an email later."""
    if payout.is_partner_payout:
        method = payout_method_for_partner(payout.delivery_partner)
        items = payout.items.select_related("shipment").order_by("shipment_id")
        recipient = payout.delivery_partner
    else:
        method = PayoutMethod.objects.filter(
            user=payout.farmer.user, kind=payout.method
        ).first()
        items = payout.items.select_related("order__listing__crop").order_by("order_id")
        recipient = payout.farmer
    return {
        "payout": payout,
        "farmer": None if payout.is_partner_payout else payout.farmer,
        "partner": payout.delivery_partner if payout.is_partner_payout else None,
        "recipient": recipient,
        "recipient_name": payout.recipient_name,
        "items": items,
        "destination": method.display if method else "No payout details on file",
        "is_verified": bool(method and method.is_verified),
    }
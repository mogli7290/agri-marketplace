"""Order lifecycle logic shared by the web views and the API.

Keeping transitions in one place guarantees the web UI, DRF endpoints and the
payment webhook all enforce the same rules.
"""

from __future__ import annotations

import logging
from decimal import Decimal, ROUND_HALF_UP

from django.conf import settings
from django.db import transaction

from marketplace.models import Listing, Order, OrderStatusHistory, Payment
from marketplace.services import notifications

logger = logging.getLogger(__name__)


class OrderError(Exception):
    """Raised for invalid order operations (bad quantity, invalid transition...)."""


#: Statuses in which an order may still take a payment. A cancelled order, or one
#: already settled, must never accept a new charge — the payment services check
#: this before talking to any gateway.
PAYABLE_STATUSES = frozenset({"placed", "confirmed", "picked_up", "in_transit", "delivered"})


def ensure_payable(order: Order) -> None:
    """Raise ``OrderError`` unless the order may still take a payment."""
    if order.payment_status in {"paid", "refunded"}:
        raise OrderError("This order has already been settled.")
    if order.status not in PAYABLE_STATUSES:
        raise OrderError("This order can no longer be paid.")


def calculate_platform_fee(subtotal: Decimal, payment_route: str = "platform") -> Decimal:
    """The platform's cut.

    Zero on direct transfers: when the buyer pays the farmer's own UPI ID the
    platform never handles the money, so there is nothing to take a fee from.
    This is the deliberate trade-off of offering a direct route — the farmer
    keeps 100% and the marketplace earns nothing on that order.
    """
    if payment_route == "direct":
        return Decimal("0.00")
    rate = Decimal(str(settings.PLATFORM_FEE_PERCENT)) / Decimal("100")
    return (subtotal * rate).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


@transaction.atomic
def switch_payment_route(order: Order, route: str, user=None) -> Order:
    """Let the buyer move an unpaid order between platform and direct payment.

    The route used to be fixed when the order was created, which meant a buyer
    who wanted to pay a farmer directly could only do so if that farmer had
    opted in by default. This makes the choice the buyer's, at the point where
    it actually matters, and shows them what each one costs them.

    Switching recomputes the platform fee rather than leaving the old one
    attached — paying a 2% fee to "save" the fee would be absurd.
    """
    from marketplace.services import deals as deals_service

    if route not in dict(Order.PAYMENT_ROUTE_CHOICES):
        raise OrderError("Unknown payment route.")

    order = Order.objects.select_for_update().select_related(
        "listing__farmer", "buyer"
    ).get(pk=order.pk)

    buyer = getattr(user, "buyer_profile", None) if user else None
    if not (user and (getattr(user, "is_staff", False) or (buyer and order.buyer_id == buyer.id))):
        raise OrderError("Only the buyer can change how this order is paid.")

    if order.payment_status in {"paid", "refunded"}:
        raise OrderError("This order has already been settled.")
    if order.status not in PAYABLE_STATUSES:
        raise OrderError("This order can no longer be paid.")
    if order.payment_route == route:
        return order

    if route == "direct":
        method = deals_service.payout_method_for(order.listing.farmer)
        if method is None or method.kind != "upi" or not method.upi_id:
            raise OrderError(
                "This farmer has not added a UPI ID, so you cannot pay them directly yet."
            )
        if Payment.objects.filter(
            order=order, provider="upi_to_farmer", status="authorized"
        ).exists():
            raise OrderError("A direct payment for this order is already awaiting confirmation.")

    subtotal = order.quantity_ordered * order.agreed_price_per_unit
    order.payment_route = route
    order.platform_fee = calculate_platform_fee(subtotal, route)
    order.save(update_fields=["payment_route", "platform_fee", "updated_at"])
    logger.info(
        "Order %s switched to %s payment%s",
        order.pk, route, f" by {user}" if user else "",
    )
    return order


@transaction.atomic
def create_order(buyer, listing: Listing, quantity: Decimal, price: Decimal | None = None,
                 payment_route: str | None = None) -> Order:
    """Place an order against a listing and reserve the stock atomically.

    ``payment_route`` is the farmer's choice of how to be paid. A ``direct`` order
    skips the platform entirely, so no platform fee is added.

    Left unset, it follows ``FarmerProfile.preferred_payment_route``. That matters
    because a buyer ordering straight off a listing never passes through the
    demand board, and the board is the only place the farmer used to get asked.
    Without this, direct orders — and the farmer's UPI QR on the order page —
    were reachable only via the board.
    """
    listing = Listing.objects.select_for_update().get(pk=listing.pk)
    if payment_route is None:
        payment_route = getattr(listing.farmer, "preferred_payment_route", "platform") or "platform"

    if not listing.is_orderable:
        raise OrderError("This listing is no longer available.")
    if quantity is None or quantity <= 0:
        raise OrderError("Quantity must be greater than zero.")
    if quantity > listing.quantity_available:
        raise OrderError("Requested quantity exceeds what is available.")
    if payment_route not in dict(Order.PAYMENT_ROUTE_CHOICES):
        raise OrderError("Unknown payment route.")

    unit_price = (price if price is not None else listing.price_per_unit).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP
    )
    if unit_price <= 0:
        raise OrderError("Price must be greater than zero.")

    subtotal = quantity * unit_price
    order = Order.objects.create(
        buyer=buyer,
        listing=listing,
        quantity_ordered=quantity,
        agreed_price_per_unit=unit_price,
        platform_fee=calculate_platform_fee(subtotal, payment_route),
        payment_route=payment_route,
        delivery_address=buyer.address,
        delivery_latitude=buyer.latitude,
        delivery_longitude=buyer.longitude,
    )

    listing.quantity_available = listing.quantity_available - quantity
    if listing.quantity_available <= 0:
        listing.quantity_available = Decimal("0")
        listing.status = "sold_out"
    listing.save(update_fields=["quantity_available", "status"])

    OrderStatusHistory.objects.create(
        order=order, status="placed", note="Order placed", changed_by=buyer.user
    )
    notifications.order_placed(order)
    return order


@transaction.atomic
def advance_status(order: Order, new_status: str, user=None, note: str = "") -> Order:
    """Move an order to ``new_status`` if the transition is permitted."""
    if not order.can_transition_to(new_status):
        raise OrderError(f"Cannot move order from '{order.status}' to '{new_status}'.")

    order.status = new_status
    update_fields = ["status", "updated_at"]
    if new_status == "paid":
        order.payment_status = "paid"
        update_fields.append("payment_status")
    order.save(update_fields=update_fields)

    OrderStatusHistory.objects.create(
        order=order, status=new_status, note=note, changed_by=user
    )
    logger.info("Order %s advanced to %s", order.pk, new_status)
    _announce_status(order, new_status)
    return order


def _announce_status(order: Order, new_status: str) -> None:
    """Email the people waiting on this transition.

    Only the two moments a buyer actually waits for get mail. Everything else is
    visible on the order page, and a notification that fires on every internal
    step is a notification people stop reading.
    """
    if new_status == "in_transit":
        notifications.order_dispatched(order)
    elif new_status == "delivered":
        notifications.order_delivered(order)


@transaction.atomic
def mark_order_paid(order: Order, payment_id: str = "", note: str = "Payment captured") -> Order:
    """Record a successful payment and move the order into ``paid``.

    Idempotent: replaying a webhook or a checkout callback leaves the order
    untouched and does not duplicate the status-history entry.
    """
    previous = order.status
    order.payment_status = "paid"
    if previous in PAYABLE_STATUSES:
        order.status = "paid"
        order.save(update_fields=["payment_status", "status", "updated_at"])
        OrderStatusHistory.objects.create(order=order, status="paid", note=note)
    else:
        order.save(update_fields=["payment_status", "updated_at"])
    logger.info("Order %s marked paid (was %s) payment=%s", order.pk, previous, payment_id)
    # Only on a genuine transition: a replayed webhook must not send a second
    # "thanks for your payment" for money already received.
    if previous != "paid":
        notifications.payment_confirmed(order)
    return order


@transaction.atomic
def mark_order_refunded(order: Order, note: str = "Payment refunded") -> Order:
    """Flag an order as refunded after the money went back to the buyer."""
    if order.payment_status == "refunded":
        return order
    order.payment_status = "refunded"
    order.save(update_fields=["payment_status", "updated_at"])
    OrderStatusHistory.objects.create(order=order, status=order.status, note=note)
    logger.info("Order %s refunded", order.pk)
    return order


def generate_invoice_number(order: Order) -> str:
    """Human-friendly, sortable invoice reference."""
    return f"AGM-{order.created_at:%Y%m}-{order.pk:06d}"

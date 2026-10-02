"""Transactional email — the thing that makes the marketplace feel alive.

A farmer on a phone is not going to open a dashboard to find out whether an order
was paid. These messages are the difference between a marketplace and a
spreadsheet.

Three rules shape this module:

* **Nothing here is allowed to raise.** A bounced email must not roll back the
  order it was about. :mod:`marketplace.services.messaging` already swallows
  delivery failures; this module never even lets a *rendering* failure escape.
* **Fire after commit.** Every notification goes out through
  ``transaction.on_commit``, so nobody is told an order was placed before the
  transaction that placed it has actually succeeded — or at all, if it rolled
  back.
* **No queue, no cost.** Sending is synchronous on the request thread. With no
  Celery and no Redis, that is the trade: a slow mail provider adds latency to
  the request that triggered it, but nothing is ever silently dropped. When
  volume justifies it, the only change needed is to swap :func:`_deliver` for a
  ``.delay()``.

Each event has a subject and a pair of templates (``email/<name>.txt`` and
``.html``). Subjects are declared here rather than in the templates so that the
whole catalogue of what the platform says can be read in one place.
"""

from __future__ import annotations

import logging

from django.conf import settings
from django.db import transaction

from marketplace.services import messaging

logger = logging.getLogger(__name__)


# Subject lines, kept together so the full set of automated mail is auditable.
SUBJECTS = {
    "order_placed": "New order #{order_id} · {site_name}",
    "payment_confirmed": "Payment received for order #{order_id} · {site_name}",
    "offer_received": "New offer on your request #{request_id} · {site_name}",
    "offer_accepted": "Your offer was accepted · {site_name}",
    "order_dispatched": "Order #{order_id} is on its way · {site_name}",
    "order_delivered": "Order #{order_id} delivered · {site_name}",
    "payout_settled": "Your payment of \u20b9{amount} is on the way · {site_name}",
    "delivery_code": "Your delivery code for order #{order_id} · {site_name}",
    "delivery_recorded": "Order #{order_id} was delivered · {site_name}",
    "delivery_confirmed": "Delivery of order #{order_id} confirmed · {site_name}",
    "delivery_disputed": "We are looking into order #{order_id} · {site_name}",
}


class NotificationError(Exception):
    """Raised when an event cannot be built (unknown template, missing subject)."""


def enabled() -> bool:
    """Master switch. ``NOTIFICATIONS_ENABLED=0`` silences everything."""
    return bool(getattr(settings, "NOTIFICATIONS_ENABLED", True))


def _base_url() -> str:
    return (getattr(settings, "SITE_URL", "") or "").rstrip("/")


def _absolute(path: str) -> str:
    """Turn a site-relative path into a link a farmer can tap on a phone."""
    return f"{_base_url()}{path}" if path else _base_url()


def _recipients(user) -> list[str]:
    """Addresses to send to, minus blanks.

    A single address today. The list shape is deliberate: adding a notification
    preference or an email-alias model later does not touch the event functions.
    """
    if user is None:
        return []
    address = (getattr(user, "email", "") or "").strip()
    return [address] if address else []


def _deliver(to: list[str], event: str, context: dict) -> int:
    """Render and send one event. Returns how many messages were accepted."""
    subject_template = SUBJECTS.get(event)
    if subject_template is None:
        raise NotificationError(f"No subject is defined for the {event!r} event.")

    context = {**context, "site_name": settings.SITE_NAME}
    subject = subject_template.format(**context)

    sent = 0
    for address in to:
        try:
            if messaging.send_email(address, subject, event, context):
                sent += 1
        except Exception:  # noqa: BLE001 - a bad template must not break the caller
            logger.exception("Could not render the %r notification for %s", event, address)
    return sent


def notify(event: str, to: list[str], context: dict, *, on_commit: bool = True) -> int:
    """Send ``event`` to ``to``.

    By default delivery is deferred to ``transaction.on_commit`` so a rolled
    back transaction never produces a message about something that did not
    happen. Pass ``on_commit=False`` for events raised outside a transaction.
    """
    # Validate the event name here, not in the callback: a typo must surface at
    # the call site, not silently at commit time.
    if event not in SUBJECTS:
        raise NotificationError(f"No subject is defined for the {event!r} event.")

    if not enabled() or not to:
        return 0

    if on_commit and transaction.get_connection().in_atomic_block:
        transaction.on_commit(lambda: _deliver(to, event, context))
        return 0

    return _deliver(to, event, context)


# ---------------------------------------------------------------------------
# Events
# ---------------------------------------------------------------------------
def order_placed(order) -> None:
    """Tell the farmer somebody wants their produce."""
    notify(
        "order_placed",
        _recipients(order.listing.farmer.user),
        {
            "order": order,
            "order_id": order.pk,
            "buyer_name": _buyer_name(order),
            "crop_name": order.listing.crop.name,
            "quantity": order.quantity_ordered,
            "unit": order.listing.crop.unit,
            "price_per_unit": order.agreed_price_per_unit,
            "amount": order.total_amount,
            "order_url": _absolute(f"/orders/{order.pk}/"),
        },
    )


def payment_confirmed(order) -> None:
    """Tell the buyer their money landed — this is the one they wait for."""
    notify(
        "payment_confirmed",
        _recipients(order.buyer.user),
        {
            "order": order,
            "order_id": order.pk,
            "amount": order.total_amount,
            "farmer_name": order.listing.farmer.full_name,
            "crop_name": order.listing.crop.name,
            "quantity": order.quantity_ordered,
            "unit": order.listing.crop.unit,
            "order_url": _absolute(f"/orders/{order.pk}/"),
        },
    )


def offer_received(offer) -> None:
    """Tell a buyer a farmer answered their demand request."""
    notify(
        "offer_received",
        _recipients(offer.request.buyer.user),
        {
            "offer": offer,
            "request_id": offer.request_id,
            "request_url": _absolute(f"/requests/{offer.request_id}/"),
            "crop_name": offer.request.crop.name,
            "quantity": offer.quantity,
            "unit": offer.request.crop.unit,
            "price_per_unit": offer.price_per_unit,
            "farmer_name": offer.farmer.full_name,
            "farmer_village": offer.farmer.village,
            "payment_route": offer.payment_route,
        },
    )


def offer_accepted(offer) -> None:
    """Tell the farmer their offer was taken up and an order now exists."""
    notify(
        "offer_accepted",
        _recipients(offer.farmer.user),
        {
            "offer": offer,
            "order_id": offer.order_id,
            "buyer_name": _buyer_name(offer.request),
            "crop_name": offer.request.crop.name,
            "quantity": offer.quantity,
            "unit": offer.request.crop.unit,
            "price_per_unit": offer.price_per_unit,
            "order_url": _absolute(f"/orders/{offer.order_id}/"),
        },
    )


def order_dispatched(order) -> None:
    """Tell the buyer their produce is on the road."""
    notify(
        "order_dispatched",
        _recipients(order.buyer.user),
        {
            "order": order,
            "order_id": order.pk,
            "crop_name": order.listing.crop.name,
            "quantity": order.quantity_ordered,
            "unit": order.listing.crop.unit,
            "farmer_name": order.listing.farmer.full_name,
            "pickup_hub": order.pickup_hub or order.listing.farmer.village,
            "order_url": _absolute(f"/orders/{order.pk}/"),
        },
    )


def order_delivered(order) -> None:
    """Tell both sides the produce arrived."""
    buyers = _recipients(order.buyer.user)
    farmers = _recipients(order.listing.farmer.user)
    notify("order_delivered", buyers + farmers, _delivery_context(order))


def payout_settled(payout) -> None:
    """Tell the recipient their statement has been paid."""
    if payout.is_partner_payout:
        partner = payout.delivery_partner
        to = _recipients(getattr(partner, "user", None))
        recipient = str(partner)
    else:
        farmer = payout.farmer
        to = _recipients(farmer.user)
        recipient = farmer.full_name
    notify(
        "payout_settled",
        to,
        {
            "payout": payout,
            "payout_id": payout.pk,
            "recipient": recipient,
            "amount": payout.net_amount,
            "period_start": payout.period_start,
            "period_end": payout.period_end,
            "reference": payout.reference,
            "items": _payout_items(payout),
            "earnings_url": _absolute(
                "/partner/earnings/" if payout.is_partner_payout else "/earnings/"
            ),
        },
    )


# ---------------------------------------------------------------------------
# Proof of delivery
# ---------------------------------------------------------------------------
def delivery_code(order, code: str, *, expires_hours: int = 24) -> None:
    """Send the buyer the code their driver will ask for.

    This is the one notification that carries a secret, so it is sent to the
    buyer alone — never to the transporter.
    """
    notify(
        "delivery_code",
        _recipients(order.buyer.user),
        {
            "order": order,
            "order_id": order.pk,
            "code": code,
            "expires_hours": expires_hours,
            "crop_name": order.listing.crop.name,
            "quantity": order.quantity_ordered,
            "unit": order.listing.crop.unit,
            "order_url": _absolute(f"/orders/{order.pk}/"),
        },
    )


def delivery_proof_recorded(order, proof) -> None:
    """Tell the buyer a delivery was claimed, and what evidence came with it."""
    notify(
        "delivery_recorded",
        _recipients(order.buyer.user),
        {
            **_delivery_context(order),
            "evidence": _evidence_labels(proof),
            "photo_url": _photo_url(proof),
            "notes": proof.notes,
        },
    )


def delivery_confirmed(order, proof) -> None:
    """Tell the transporter their delivery charge is now undisputed."""
    partner_user = getattr(getattr(proof.recorded_by, "user", None), "email", "")
    notify(
        "delivery_confirmed",
        [partner_user] if partner_user else [],
        {
            **_delivery_context(order),
            "evidence": _evidence_labels(proof),
            "shipment_url": _absolute(f"/shipments/{proof.shipment_id}/"),
        },
    )


def delivery_disputed(order, proof) -> None:
    """Tell the transporter a delivery is contested before they chase payment."""
    partner_user = getattr(getattr(proof.recorded_by, "user", None), "email", "")
    notify(
        "delivery_disputed",
        [partner_user] if partner_user else [],
        {
            **_delivery_context(order),
            "reason": proof.dispute_reason or "The buyer has not given a reason.",
            "shipment_url": _absolute(f"/shipments/{proof.shipment_id}/"),
        },
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _buyer_name(source) -> str:
    buyer = getattr(source, "buyer", None)
    if buyer is None:
        return "the buyer"
    return buyer.business_name or buyer.user.get_full_name() or "the buyer"


def _evidence_labels(proof) -> list[str]:
    labels = []
    if proof.has_otp:
        labels.append("delivery code")
    if proof.has_photo:
        labels.append("photo")
    if proof.has_signature:
        labels.append("signature")
    return labels


def _photo_url(proof) -> str:
    if not proof.has_photo:
        return ""
    try:
        return proof.photo.url
    except ValueError:  # pragma: no cover - storage with no public URL
        return ""


def _delivery_context(order) -> dict:
    return {
        "order": order,
        "order_id": order.pk,
        "crop_name": order.listing.crop.name,
        "quantity": order.quantity_ordered,
        "unit": order.listing.crop.unit,
        "farmer_name": order.listing.farmer.full_name,
        "order_url": _absolute(f"/orders/{order.pk}/"),
    }


def _payout_items(payout) -> list[dict]:
    """Plain lines for the statement email, whichever kind it is."""
    lines = []
    for item in payout.items.all():
        if item.order_id:
            lines.append({
                "label": f"Order #{item.order_id}",
                "amount": item.amount,
            })
        elif item.shipment_id:
            lines.append({
                "label": f"Shipment #{item.shipment_id}",
                "amount": item.amount,
            })
    return lines
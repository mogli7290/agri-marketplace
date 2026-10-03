"""Price-offer (negotiation) service.

A buyer proposes a price; the farmer accepts or rejects it. Accepting converts
the offer into a real :class:`Order` at the agreed price, re-checking stock at
that moment (stock may have moved since the offer was made).

Kept out of the views so the web UI and the API enforce identical rules.
"""

from __future__ import annotations

import logging
from decimal import Decimal, ROUND_HALF_UP

from django.db import transaction
from django.utils import timezone

from marketplace.models import Listing, PriceOffer
from marketplace.services import orders as order_service

logger = logging.getLogger(__name__)


class OfferError(Exception):
    """Raised for invalid offer operations."""


def _q(value: Decimal) -> Decimal:
    # Accept a float without AttributeError, and without the binary-expansion
    # rounding that Decimal(float) would introduce.
    if not isinstance(value, Decimal):
        value = Decimal(str(value))
    return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


@transaction.atomic
def create_offer(listing: Listing, buyer, quantity: Decimal, proposed_price: Decimal,
                 message: str = "") -> PriceOffer:
    """Place a price offer on a listing. Does not reserve stock."""
    listing = Listing.objects.select_for_update().get(pk=listing.pk)

    if not listing.is_orderable:
        raise OfferError("This listing is no longer available.")
    if listing.farmer.user_id == getattr(buyer, "user_id", None) or listing.farmer.user_id == getattr(
        getattr(buyer, "user", None), "id", None
    ):
        raise OfferError("You cannot make an offer on your own listing.")
    if quantity is None or quantity <= 0:
        raise OfferError("Quantity must be greater than zero.")
    if quantity > listing.quantity_available:
        raise OfferError("Requested quantity exceeds what is available.")
    if proposed_price is None or proposed_price <= 0:
        raise OfferError("Offered price must be greater than zero.")

    if PriceOffer.objects.filter(listing=listing, buyer=buyer, status="pending").exists():
        raise OfferError("You already have a pending offer on this listing.")

    offer = PriceOffer.objects.create(
        listing=listing,
        buyer=buyer,
        quantity_requested=quantity,
        proposed_price=_q(proposed_price),
        message=(message or "")[:500],
    )
    logger.info("Offer %s created on listing %s by buyer %s", offer.pk, listing.pk, buyer.pk)
    return offer


@transaction.atomic
def respond_offer(offer: PriceOffer, action: str, user, note: str = "") -> PriceOffer:
    """Accept or reject an offer (farmer or staff only).

    ``accept`` also creates the order at the proposed price.
    """
    locked = PriceOffer.objects.select_for_update().select_related(
        "listing__farmer", "buyer"
    ).get(pk=offer.pk)

    if locked.status != "pending":
        raise OfferError("This offer is no longer pending.")

    is_farmer = locked.listing.farmer.user_id == user.id
    if not (is_farmer or user.is_staff):
        raise OfferError("Only the seller can respond to this offer.")

    locked.responded_at = timezone.now()

    if action == "reject":
        locked.status = "rejected"
        locked.response_note = (note or "Offer declined")[:255]
        locked.save(update_fields=["status", "response_note", "responded_at", "updated_at"])
        logger.info("Offer %s rejected", locked.pk)
        return locked

    if action == "accept":
        try:
            order = order_service.create_order(
                buyer=locked.buyer,
                listing=locked.listing,
                quantity=locked.quantity_requested,
                price=locked.proposed_price,
            )
        except order_service.OrderError as exc:
            raise OfferError(str(exc)) from exc
        locked.status = "accepted"
        locked.order = order
        locked.response_note = (note or "Offer accepted")[:255]
        locked.save(update_fields=["status", "order", "response_note", "responded_at", "updated_at"])
        logger.info("Offer %s accepted → order %s", locked.pk, order.pk)
        return locked

    raise OfferError(f"Unknown offer action '{action}'.")


@transaction.atomic
def cancel_offer(offer: PriceOffer, user) -> PriceOffer:
    """Withdraw a pending offer (buyer only)."""
    if offer.status != "pending":
        raise OfferError("Only pending offers can be withdrawn.")
    if offer.buyer.user_id != user.id and not user.is_staff:
        raise OfferError("Only the buyer can withdraw this offer.")
    offer.status = "cancelled"
    offer.save(update_fields=["status", "updated_at"])
    return offer

"""The demand board: buyers post what they need, sellers come to them.

This is the marketplace's other half. A listing is supply-first — the farmer
publishes and waits. A :class:`~marketplace.models.DemandRequest` is
demand-first — a buyer says "I want 100 kg of tomato in Pune by Friday" and
farmers compete for the business.

The deal flow:

    DemandRequest ──RequestOffer──▶ buyer accepts ──▶ Order + conversation
         │                                                        │
         └── conversation opens on the first offer ───────────────┘
                                                                  │
                              contact details unlock only here ────┘

Contact details are deliberately withheld until an order exists, so the board is
not a phone-number directory and sellers cannot spam buyers who never
engaged.
"""

from __future__ import annotations

import logging
import re
from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from marketplace.models import (
    BuyerProfile,
    Conversation,
    DemandRequest,
    FarmerProfile,
    Listing,
    Message,
    Order,
    PayoutMethod,
    RequestOffer,
)
from marketplace.services import notifications
from marketplace.services import orders as order_service

logger = logging.getLogger(__name__)


class DealError(Exception):
    """Raised for invalid demand-board operations."""


def _q(value: Decimal) -> Decimal:
    # str() so a float keeps the precision the caller wrote rather than its
    # binary expansion (2.675 would otherwise round down to 2.67).
    return Decimal(str(value)).quantize(Decimal("0.01"))


# ---------------------------------------------------------------------------
# Payout details
# ---------------------------------------------------------------------------
def save_payout_method(user, *, kind: str, upi_id: str = "", account_holder: str = "",
                       account_number: str = "", ifsc: str = "") -> PayoutMethod:
    """Create or update the user's primary payout destination.

    Only payout destinations live here. Nothing in this module takes money
    *from* a user; it is where money is sent *to* them.
    """
    from marketplace.services import upi

    if kind not in dict(PayoutMethod.KIND_CHOICES):
        raise DealError("Choose either a UPI ID or a bank account.")

    upi_id = (upi_id or "").strip()
    account_number = re.sub(r"\s+", "", account_number or "")
    ifsc = (ifsc or "").strip().upper()

    if kind == "upi":
        if not upi.is_valid_vpa(upi_id):
            raise DealError("That does not look like a UPI ID (it should look like name@bank).")
        account_number, ifsc, account_holder = "", "", ""
    else:
        if not account_holder:
            raise DealError("Enter the account holder's name.")
        if not (6 <= len(account_number) <= 18) or not account_number.isdigit():
            raise DealError("Enter a valid account number (6-18 digits).")
        if not (ifsc and len(ifsc) == 11 and ifsc[:4].isalpha() and ifsc[4:].isalnum()):
            raise DealError("Enter a valid IFSC code, e.g. HDFC0001234.")

    details = {
        "upi_id": upi_id,
        "account_holder": account_holder,
        "account_number": account_number,
        "ifsc": ifsc,
    }
    existing = PayoutMethod.objects.filter(user=user, kind=kind).first()
    method, _ = PayoutMethod.objects.update_or_create(
        user=user, kind=kind,
        defaults={**details, "is_primary": True},
    )
    _revoke_stale_verification(method, _details_changed(existing, **details))
    logger.info("Saved %s payout method for %s", kind, user)
    return method


def _details_changed(existing, **new_details) -> bool:
    """Whether a save would alter any field a verification was made against.

    Compared against the row as it stands *before* the write, because
    ``update_or_create`` has already put the new values on it by the time it
    returns.
    """
    if existing is None:
        return False
    return any(getattr(existing, field) != value for field, value in new_details.items())


def _revoke_stale_verification(method, details_changed: bool) -> None:
    """Changing the details drops a check made against the old ones.

    Verification says "we looked at *this* address and it matched this
    farmer's name". A farmer who was checked once and then swapped the UPI ID
    would otherwise keep the badge on an address nobody ever looked at, which
    turns the one signal buyers are told to rely on into decoration.
    """
    if not (details_changed and method.is_verified):
        return
    method.is_verified = False
    method.verified_at = None
    method.verified_by = None
    method.save(
        update_fields=["is_verified", "verified_at", "verified_by", "updated_at"]
    )
    logger.warning("Payout method %s changed, so its verification was revoked", method.pk)


def payout_method_for(farmer: FarmerProfile) -> PayoutMethod | None:
    """The farmer's verified-or-not primary destination."""
    return PayoutMethod.objects.filter(user=farmer.user, is_primary=True).first()


def save_partner_payout_method(partner, *, kind: str, upi_id: str = "",
                               account_holder: str = "", account_number: str = "",
                               ifsc: str = "") -> PayoutMethod:
    """Same validation as a farmer's destination, stored against the partner.

    Transporters are not users in the marketplace sense, so their destination is
    keyed on the :class:`~marketplace.models.DeliveryPartner` row instead of a
    ``User``.
    """
    from marketplace.services import upi

    if kind not in dict(PayoutMethod.KIND_CHOICES):
        raise DealError("Choose either a UPI ID or a bank account.")

    upi_id = (upi_id or "").strip()
    account_number = re.sub(r"\s+", "", account_number or "")
    ifsc = (ifsc or "").strip().upper()

    if kind == "upi":
        if not upi.is_valid_vpa(upi_id):
            raise DealError("That does not look like a UPI ID (it should look like name@bank).")
        account_number, ifsc, account_holder = "", "", ""
    else:
        if not account_holder:
            raise DealError("Enter the account holder's name.")
        if not (6 <= len(account_number) <= 18) or not account_number.isdigit():
            raise DealError("Enter a valid account number (6-18 digits).")
        if not (ifsc and len(ifsc) == 11 and ifsc[:4].isalpha() and ifsc[4:].isalnum()):
            raise DealError("Enter a valid IFSC code, e.g. HDFC0001234.")

    details = {
        "upi_id": upi_id,
        "account_holder": account_holder,
        "account_number": account_number,
        "ifsc": ifsc,
    }
    existing = PayoutMethod.objects.filter(delivery_partner=partner, kind=kind).first()
    method, _ = PayoutMethod.objects.update_or_create(
        delivery_partner=partner, kind=kind,
        defaults={**details, "is_primary": True},
    )
    _revoke_stale_verification(method, _details_changed(existing, **details))
    logger.info("Saved %s payout method for partner %s", kind, partner)
    return method


def partner_payout_method_for(partner) -> PayoutMethod | None:
    """The transporter's primary payout destination, if one is on file."""
    return PayoutMethod.objects.filter(delivery_partner=partner, is_primary=True).first()


def can_take_direct_payments(farmer: FarmerProfile) -> bool:
    """A farmer must have a UPI ID on file before buyers can pay them directly.

    There is nobody to pay otherwise, so the direct route is simply unavailable
    rather than half-broken.
    """
    method = payout_method_for(farmer)
    return method is not None and method.kind == "upi" and bool(method.upi_id)


# ---------------------------------------------------------------------------
# Requests
# ---------------------------------------------------------------------------
@transaction.atomic
def create_request(buyer, *, crop, quantity: Decimal, delivery_city: str,
                   target_price: Decimal | None = None, needed_by=None, notes: str = "") -> DemandRequest:
    """Post a public "I need this much of this" request."""
    if quantity is None or quantity <= 0:
        raise DealError("Quantity must be greater than zero.")
    if not (delivery_city or "").strip():
        raise DealError("Enter the city you need delivery in.")
    if target_price is not None and target_price <= 0:
        raise DealError("Target price must be greater than zero.")

    request = DemandRequest.objects.create(
        buyer=buyer,
        crop=crop,
        quantity=_q(quantity),
        target_price=_q(target_price) if target_price is not None else None,
        delivery_city=delivery_city.strip()[:150],
        needed_by=needed_by,
        notes=(notes or "")[:2000],
    )
    logger.info("Demand request %s posted by %s", request.pk, buyer)
    return request


def close_request(request: DemandRequest, user=None) -> DemandRequest:
    """Buyer withdraws their request."""
    owner_id = request.buyer_id
    if user is not None:
        buyer = getattr(user, "buyer_profile", None)
        if not (user.is_staff or (buyer and buyer.id == owner_id)):
            raise DealError("Only the buyer can close this request.")
    if not request.is_open:
        raise DealError("This request can no longer be closed.")
    request.status = "cancelled"
    request.save(update_fields=["status", "updated_at"])
    RequestOffer.objects.filter(request=request, status="pending").update(
        status="rejected", responded_at=timezone.now()
    )
    return request


# ---------------------------------------------------------------------------
# Offers
# ---------------------------------------------------------------------------
def open_requests_for(farmer: FarmerProfile, crop=None):
    """Open requests a farmer can respond to, newest first.

    ``crop`` narrows the board to produce they actually sell; without it they
    see everything that is still open.
    """
    queryset = DemandRequest.objects.filter(status="open").select_related(
        "buyer__user", "crop"
    )
    if crop is not None:
        queryset = queryset.filter(crop=crop)
    return queryset


@transaction.atomic
def create_offer(request: DemandRequest, farmer: FarmerProfile, *, price_per_unit: Decimal,
                 quantity: Decimal | None = None, payment_route: str = "platform",
                 message: str = "") -> RequestOffer:
    """A farmer replies to a request, opening the conversation.

    ``payment_route`` is the farmer's choice: ``platform`` means they want the
    marketplace to collect and settle (and pay the fee), ``direct`` means the
    buyer pays their own UPI ID and they keep everything.
    """
    request = DemandRequest.objects.select_for_update().get(pk=request.pk)

    if not request.is_open:
        raise DealError("This request is closed.")
    if request.buyer.user_id == farmer.user_id:
        raise DealError("You cannot respond to your own request.")
    if payment_route not in dict(Order.PAYMENT_ROUTE_CHOICES):
        raise DealError("Unknown payment route.")
    if payment_route == "direct" and not can_take_direct_payments(farmer):
        raise DealError("Add a UPI ID to your profile before offering direct payment.")

    quantity = _q(quantity if quantity is not None else request.quantity)
    if quantity <= 0:
        raise DealError("Quantity must be greater than zero.")
    if price_per_unit is None or price_per_unit <= 0:
        raise DealError("Price must be greater than zero.")

    if RequestOffer.objects.filter(request=request, farmer=farmer, status="pending").exists():
        raise DealError("You already have an open offer on this request.")

    offer = RequestOffer.objects.create(
        request=request,
        farmer=farmer,
        quantity=quantity,
        price_per_unit=_q(price_per_unit),
        payment_route=payment_route,
        message=(message or "")[:500],
    )
    conversation = get_or_create_conversation(request, farmer)
    if message:
        Message.objects.create(
            conversation=conversation, sender=farmer.user, body=message[:2000]
        )
        conversation.last_message_at = timezone.now()
        conversation.save(update_fields=["last_message_at", "updated_at"])

    logger.info("Request offer %s on request %s by %s", offer.pk, request.pk, farmer)
    notifications.offer_received(offer)
    return offer


@transaction.atomic
def respond_offer(offer: RequestOffer, action: str, user) -> RequestOffer:
    """Buyer accepts or rejects a farmer's offer.

    Accepting reserves stock against one of the farmer's listings and opens a
    real order — the farmer needs a listing to sell from.
    """
    caller = offer
    offer = (
        RequestOffer.objects.select_for_update()
        .select_related("request__buyer__user", "farmer__user", "order")
        .get(pk=offer.pk)
    )
    if offer.status != "pending":
        raise DealError("This offer is no longer open.")
    if offer.request.buyer_id != buyer_id_for(user) and not user.is_staff:
        raise DealError("Only the buyer can respond to this offer.")

    if action == "reject":
        offer.status = "rejected"
        offer.responded_at = timezone.now()
        offer.save(update_fields=["status", "responded_at", "updated_at"])
        caller.refresh_from_db()
        return offer

    if action != "accept":
        raise DealError(f"Unknown action '{action}'.")

    locked = offer
    listing = (
        Listing.objects.select_for_update()
        .filter(farmer=locked.farmer, crop=locked.request.crop, status="active")
        .order_by("-created_at")
        .first()
    )
    if listing is None:
        raise DealError(
            "This farmer has no active listing for that crop, so an order cannot be "
            "created yet. Ask them to publish one."
        )

    try:
        order = order_service.create_order(
            buyer=locked.request.buyer,
            listing=listing,
            quantity=locked.quantity,
            price=locked.price_per_unit,
            payment_route=locked.payment_route,
        )
    except order_service.OrderError as exc:
        raise DealError(str(exc)) from exc

    locked.status = "accepted"
    locked.order = order
    locked.responded_at = timezone.now()
    locked.save(update_fields=["status", "order", "responded_at", "updated_at"])

    locked.request.status = "fulfilled"
    locked.request.save(update_fields=["status", "updated_at"])
    RequestOffer.objects.filter(request=locked.request, status="pending").exclude(
        pk=locked.pk
    ).update(status="rejected", responded_at=timezone.now())

    # The deal is struck, so contact details unlock in the thread.
    conversation = get_or_create_conversation(locked.request, locked.farmer)
    if conversation.order_id != order.pk:
        conversation.order = order
        conversation.save(update_fields=["order", "updated_at"])

    logger.info("Request offer %s accepted → order %s", locked.pk, order.pk)
    notifications.offer_accepted(locked)
    # Keep the caller's copy in step — views read ``offer.order_id`` afterwards.
    caller.refresh_from_db()
    return locked


def withdraw_offer(offer: RequestOffer, user) -> RequestOffer:
    """Farmer pulls their offer back."""
    if offer.status != "pending":
        raise DealError("Only open offers can be withdrawn.")
    if offer.farmer.user_id != user.id and not user.is_staff:
        raise DealError("Only the farmer can withdraw this offer.")
    offer.status = "withdrawn"
    offer.save(update_fields=["status", "updated_at"])
    return offer


def buyer_id_for(user):
    buyer = getattr(user, "buyer_profile", None)
    return buyer.id if buyer else None


# ---------------------------------------------------------------------------
# Conversations
# ---------------------------------------------------------------------------
def get_or_create_conversation(request: DemandRequest, farmer: FarmerProfile) -> Conversation:
    conversation, _ = Conversation.objects.get_or_create(
        request=request, farmer=farmer, defaults={"buyer": request.buyer}
    )
    return conversation


def get_or_create_listing_conversation(listing, buyer: BuyerProfile) -> Conversation:
    """Open (or rejoin) the thread between a buyer and the seller of a listing.

    This is the other way into messaging. The board can only be entered by a
    buyer who posts a public need, so a buyer browsing a listing had no way to
    ask a seller a question — they could only order or bid.

    Clicking twice reopens the same thread rather than starting a second one,
    which is what the unique constraint on (listing, buyer) guarantees.
    """
    conversation, _ = Conversation.objects.get_or_create(
        listing=listing, buyer=buyer, defaults={"farmer": listing.farmer}
    )
    return conversation


def conversations_for(user):
    """Every thread the user takes part in, most recently active first."""
    queryset = Conversation.objects.select_related("buyer__user", "farmer__user", "request")
    buyer = getattr(user, "buyer_profile", None)
    farmer = getattr(user, "farmer_profile", None)
    if buyer:
        queryset = queryset.filter(buyer=buyer)
    elif farmer:
        queryset = queryset.filter(farmer=farmer)
    else:
        queryset = queryset.none()
    return queryset


@transaction.atomic
def post_message(conversation: Conversation, user, body: str) -> Message:
    """Add a message. Both participants may write."""
    body = (body or "").strip()
    if not body:
        raise DealError("Write something first.")
    if len(body) > 2000:
        raise DealError("That message is too long (2000 characters max).")

    buyer = getattr(user, "buyer_profile", None)
    farmer = getattr(user, "farmer_profile", None)
    if not (
        (buyer and conversation.buyer_id == buyer.id)
        or (farmer and conversation.farmer_id == farmer.id)
        or user.is_staff
    ):
        raise DealError("You are not part of this conversation.")

    message = Message.objects.create(
        conversation=conversation, sender=user, body=body[:2000]
    )
    conversation.last_message_at = message.created_at
    conversation.save(update_fields=["last_message_at", "updated_at"])
    return message


def contact_details(conversation: Conversation, user) -> dict | None:
    """Phone numbers and a WhatsApp link, gated per side.

    The gate is deliberately **asymmetric**, because the two sides consented to
    different things:

    * A **buyer** who posted a request invited farmers to answer it. As soon as
      one actually makes an offer they have engaged on purpose, so the buyer can
      reach that farmer — a pending offer is enough.
    * A **buyer**'s number stays hidden from a farmer until an order exists.
      Otherwise any farmer could post a one-rupee spam offer to harvest every
      buyer's number off a public board, which is precisely what the demand
      board must not become.
    * **Staff** can always see both, for support and dispute handling.

    Returns ``None`` while locked, so templates cannot accidentally leak a
    number.
    """
    buyer = getattr(user, "buyer_profile", None)
    farmer = getattr(user, "farmer_profile", None)
    is_staff = user.is_staff

    is_buyer_side = bool(is_staff or (buyer and conversation.buyer_id == buyer.id))
    is_farmer_side = bool(farmer and conversation.farmer_id == farmer.id)
    if not (is_buyer_side or is_farmer_side):
        return None

    if is_buyer_side:
        unlocked = conversation.deal_struck or _has_offer(conversation)
        counterparty, who = conversation.farmer, "farmer"
    else:
        unlocked = conversation.deal_struck
        counterparty, who = conversation.buyer, "buyer"

    if not unlocked:
        return None

    phone = (getattr(counterparty, "phone_number", "") or "").strip()
    return {
        "role": who,
        "name": _display_name(counterparty),
        "phone": phone,
        "whatsapp": f"https://wa.me/{phone.lstrip('+')}" if phone else "",
        "why": (
            "deal agreed"
            if conversation.deal_struck
            else "this farmer answered your request"
        ),
    }


def order_contact(order: Order, user) -> dict | None:
    """Contact details for the other side of an order.

    An order *is* the deal, so unlike the board there is nothing left to protect
    here: both sides have committed, and a farmer who cannot call their buyer to
    arrange a pickup has a real problem. Staff see it too.
    """
    buyer = getattr(user, "buyer_profile", None)
    farmer = getattr(user, "farmer_profile", None)
    is_staff = user.is_staff

    if is_staff or (buyer and order.buyer_id == buyer.id):
        counterparty, who = order.listing.farmer, "farmer"
    elif farmer and order.listing.farmer_id == farmer.id:
        counterparty, who = order.buyer, "buyer"
    else:
        return None

    phone = (getattr(counterparty, "phone_number", "") or "").strip()
    return {
        "role": who,
        "name": _display_name(counterparty),
        "phone": phone,
        "whatsapp": f"https://wa.me/{phone.lstrip('+')}" if phone else "",
        "village": getattr(counterparty, "village", "") if who == "farmer" else "",
        "city": getattr(counterparty, "city", "") if who == "buyer" else "",
        "why": "order placed",
    }


def _has_offer(conversation: Conversation) -> bool:
    """Did this farmer put an offer on the buyer's request?"""
    if conversation.request_id is None or conversation.farmer_id is None:
        return False
    return RequestOffer.objects.filter(
        request_id=conversation.request_id,
        farmer_id=conversation.farmer_id,
    ).exclude(status="rejected").exists()


def _display_name(profile) -> str:
    """A person's name, falling back to something better than their phone number."""
    name = getattr(profile, "full_name", "") or getattr(profile, "business_name", "")
    name = (name or "").strip()
    if name:
        return name
    user = getattr(profile, "user", None)
    full_name = (getattr(user, "get_full_name", lambda: "")() or "").strip()
    return full_name or "the other party"
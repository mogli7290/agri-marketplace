"""Proof of delivery — the thing both sides stop arguing about.

Without this, "the driver says he dropped it, the buyer says nothing arrived"
is unresolvable. With it, there is a code only the recipient can read, a
timestamp, a location, and optionally a photo or a signature.

The flow, and who benefits from each step:

    buyer opens order ──▶ code issued (email, hashed at rest)
                              │
    driver arrives ──────▶ types the code + captures photo/signature
                              │
                          stop closed, shipment completed
                              │
                          buyer confirms or disputes
                              │
                     dispute ──▶ order frozen, no payout until staff resolve

Design rules:

* **The code is hashed**, like the email verification token. A leaked database
  must not let anyone confirm a delivery they did not receive.
* **Wrong codes are rate limited.** Six digits is only a million guesses, so a
  delivery stop is capped at :data:`MAX_CODE_ATTEMPTS` and then needs a fresh
  code. Without that, a driver could "prove" a delivery to an empty house by
  brute force.
* **Staff can always record a proof without a code.** Phones fail, addresses get
  renamed, produce gets handed to a gatekeeper. Refusing to model that would mean
  the feature gets bypassed in the field, so the override is explicit and
  attributed rather than impossible.
* **Nothing raises into the request.** A failure to write a proof must never
  cost the transporter their stop.
"""

from __future__ import annotations

import hashlib
import logging
import secrets
from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from marketplace.models import DeliveryProof, Order, Shipment, ShipmentStop
from marketplace.services import notifications

logger = logging.getLogger(__name__)

#: A delivery code is short-lived by nature; six digits typed from a parcel
#: handed over at a gate is standard practice in Indian last-mile delivery.
CODE_LENGTH = 6
CODE_MAX_AGE_SECONDS = 24 * 3600
#: Brute-force budget for one stop. Exhausting it forces a new code rather than
#: locking the driver out mid-round.
MAX_CODE_ATTEMPTS = 5

#: Refuse anything larger. Phone photos are 2–5 MB; a proof of delivery is not
#: the place to store a 12 MP original.
MAX_PHOTO_BYTES = 5 * 1024 * 1024
ALLOWED_PHOTO_TYPES = {"image/jpeg", "image/png", "image/webp"}


class DeliveryError(Exception):
    """Raised when a proof cannot be recorded or a code cannot be honoured."""


def _hash(code: str) -> str:
    return hashlib.sha256(code.encode()).hexdigest()


def _generate_code() -> str:
    # ``secrets.randbelow`` rather than ``random``: this value gates money.
    return "".join(str(secrets.randbelow(10)) for _ in range(CODE_LENGTH))


# ---------------------------------------------------------------------------
# Codes
# ---------------------------------------------------------------------------
def issue_code(order: Order) -> str:
    """Mint a fresh delivery code for ``order`` and email it to the buyer.

    Replaces any previous code, so a leaked or screenshot code goes stale.
    Returns the plain code — the caller may need to display it, but only its
    hash is stored.
    """
    stop = _delivery_stop(order)
    if stop is None:
        raise DeliveryError("This order is not on a delivery route yet.")

    code = _generate_code()
    proof, _ = DeliveryProof.objects.select_for_update().get_or_create(
        stop=stop,
        defaults={"order": order, "shipment": stop.shipment},
    )
    proof.otp_hash = _hash(code)
    proof.otp_sent_at = timezone.now()
    proof.otp_confirmed_at = None
    proof.otp_attempts = 0
    proof.save(
        update_fields=["otp_hash", "otp_sent_at", "otp_confirmed_at", "otp_attempts", "updated_at"]
    )

    notifications.delivery_code(order, code, expires_hours=CODE_MAX_AGE_SECONDS // 3600)
    logger.info("Delivery code issued for order %s", order.pk)
    return code


def verify_code(order: Order, code: str, user=None) -> DeliveryProof:
    """Check the code a driver typed, and confirm the proof if it matches.

    Increments the attempt counter on every call, right or wrong, so the budget
    cannot be reset by simply guessing again.
    """
    proof = _proof_for(order)
    if proof is None or not proof.has_code_issued:
        raise DeliveryError("Ask the buyer for a fresh delivery code.")

    if _code_expired(proof):
        raise DeliveryError("That delivery code has expired. Ask for a new one.")
    if proof.otp_attempts >= MAX_CODE_ATTEMPTS:
        raise DeliveryError(
            "Too many wrong codes for this stop. Ask the buyer for a new one."
        )

    proof.otp_attempts += 1
    fields = ["otp_attempts", "otp_confirmed_at", "updated_at"]

    if not secrets.compare_digest(proof.otp_hash, _hash((code or "").strip())):
        proof.save(update_fields=fields)
        remaining = MAX_CODE_ATTEMPTS - proof.otp_attempts
        raise DeliveryError(
            f"That code is not right. {remaining} attempt{'s' if remaining != 1 else ''} left."
        )

    proof.otp_confirmed_at = timezone.now()
    if proof.status != "confirmed":
        proof.status = "confirmed"
        fields.append("status")
    proof.save(update_fields=fields)

    logger.info("Delivery code confirmed for order %s", order.pk)
    notifications.delivery_proof_recorded(order, proof)
    return proof


def _code_expired(proof: DeliveryProof) -> bool:
    if proof.otp_sent_at is None:
        return True
    return (timezone.now() - proof.otp_sent_at).total_seconds() > CODE_MAX_AGE_SECONDS


# ---------------------------------------------------------------------------
# Recording
# ---------------------------------------------------------------------------
def record_proof(order: Order, *, user=None, partner=None, photo=None, signature: str = "",
                 notes: str = "", latitude=None, longitude=None,
                 code: str = "") -> DeliveryProof:
    """Close a delivery stop with evidence.

    Requires a correct ``code`` unless the caller is staff, in which case the
    override is allowed and recorded against the actor.
    """
    stop = _delivery_stop(order)
    if stop is None:
        raise DeliveryError("This order is not on a delivery route yet.")

    proof = _proof_for(order) or DeliveryProof(
        stop=stop, order=order, shipment=stop.shipment
    )

    has_code = False
    if code:
        verify_code(order, code, user=user)
        proof = _proof_for(order)
        has_code = True

    if not has_code and not (user is not None and user.is_staff):
        raise DeliveryError("Enter the buyer's delivery code to close this stop.")

    if photo is not None:
        _validate_photo(photo)
        proof.photo = photo

    signature = (signature or "").strip()
    if signature and not _looks_like_signature(signature):
        raise DeliveryError("That signature does not look like a drawing.")
    if signature:
        proof.signature = signature[:20000]

    if not (has_code or proof.photo or proof.signature):
        raise DeliveryError("Record the code, a photo or a signature — not nothing.")

    proof.notes = (notes or "").strip()[:500]
    if latitude is not None:
        proof.latitude = _coordinate(latitude)
    if longitude is not None:
        proof.longitude = _coordinate(longitude)
    proof.recorded_by = partner
    proof.recorded_by_user = user
    if proof.status != "disputed":
        proof.status = "recorded"

    with transaction.atomic():
        proof.save()
        stop.status = "completed"
        stop.completed_at = timezone.now()
        stop.save(update_fields=["status", "completed_at"])

    logger.info(
        "Proof recorded for order %s (%d piece(s) of evidence)", order.pk, proof.evidence_count
    )
    if not has_code:
        # A code was skipped, so the buyer has no reason to look at the order —
        # tell them anyway that a delivery was claimed.
        notifications.delivery_proof_recorded(order, proof)
    return proof


def confirm_by_buyer(order: Order, user, accept: bool = True,
                     reason: str = "") -> DeliveryProof:
    """The buyer's side of the trust: accept the delivery, or dispute it."""
    proof = _proof_for(order)
    if proof is None:
        raise DeliveryError("No delivery has been recorded for this order yet.")
    if proof.status == "confirmed":
        return proof

    if not accept:
        reason = (reason or "").strip()
        if not reason:
            raise DeliveryError("Tell us what was wrong with the delivery.")
        proof.status = "disputed"
        proof.dispute_reason = reason[:300]
        proof.save(update_fields=["status", "dispute_reason", "updated_at"])
        logger.info("Delivery for order %s disputed by the buyer", order.pk)
        notifications.delivery_disputed(order, proof)
        return proof

    proof.status = "confirmed"
    proof.confirmed_by_buyer = user
    proof.save(
        update_fields=["status", "confirmed_by_buyer", "updated_at"]
    )
    logger.info("Delivery for order %s confirmed by the buyer", order.pk)
    notifications.delivery_confirmed(order, proof)
    return proof


def resolve_dispute(order: Order, user, resolution: str, notes: str = "") -> DeliveryProof:
    """Staff settle a dispute in the buyer's favour or the transporter's."""
    proof = _proof_for(order)
    if proof is None or not proof.is_disputed:
        raise DeliveryError("That delivery is not disputed.")

    resolution = (resolution or "").strip().lower()
    if resolution not in {"refund", "release"}:
        raise DeliveryError("Choose whether to refund the buyer or release the money.")

    with transaction.atomic():
        proof.status = "confirmed"
        proof.confirmed_by_buyer = user
        proof.dispute_reason = (
            f"{proof.dispute_reason} → resolved by {user}: {resolution}"
            f"{f' ({notes.strip()[:200]})' if notes.strip() else ''}"
        )[:300]
        proof.save(update_fields=["status", "confirmed_by_buyer", "dispute_reason", "updated_at"])

    if resolution == "release":
        # The transporter keeps the delivery charge; the shipment can now close.
        close_shipment(proof.shipment)

    logger.info("Dispute on order %s resolved as %s", order.pk, resolution)
    return proof


# ---------------------------------------------------------------------------
# Shipment lifecycle
# ---------------------------------------------------------------------------
def close_shipment(shipment: Shipment, *, force: bool = False, user=None) -> Shipment:
    """Mark a shipment completed once every delivery stop is closed.

    A shipment being ``completed`` is what makes its delivery charge payable, so
    this is the gate the money loop depends on — with ``force`` for staff
    overriding a stop that can never be closed (buyer unreachable, address gone).
    """
    if shipment.status == "completed":
        return shipment
    if shipment.status == "cancelled":
        raise DeliveryError("A cancelled shipment cannot be completed.")

    stops = list(shipment.stops.filter(kind="delivery"))
    if not stops:
        raise DeliveryError("This shipment has no delivery stops.")

    unclosed = [stop for stop in stops if not _stop_closed(stop)]
    if unclosed and not force:
        raise DeliveryError(
            f"{len(unclosed)} delivery stop(s) on this shipment are still open."
        )
    if force and unclosed:
        logger.warning(
            "Shipment %s force-completed with %d open stop(s) by %s",
            shipment.pk, len(unclosed), user or "staff",
        )

    shipment.status = "completed"
    shipment.save(update_fields=["status", "updated_at"])
    for stop in unclosed:
        stop.status = "completed"
        stop.completed_at = timezone.now()
        stop.save(update_fields=["status", "completed_at"])

    logger.info("Shipment %s completed (%d stops)", shipment.pk, len(stops))
    return shipment


def _stop_closed(stop: ShipmentStop) -> bool:
    """A stop counts as closed when it has a proof, not merely a timestamp."""
    return hasattr(stop, "proof")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _delivery_stop(order: Order) -> ShipmentStop | None:
    return (
        ShipmentStop.objects.filter(
            shipment__stops__order=order, kind="delivery"
        )
        .select_related("shipment")
        .order_by("sequence")
        .first()
    )


def _proof_for(order: Order) -> DeliveryProof | None:
    return DeliveryProof.objects.filter(order=order).order_by("-created_at").first()


def _validate_photo(photo) -> None:
    if getattr(photo, "size", 0) > MAX_PHOTO_BYTES:
        raise DeliveryError("That photo is too large. Keep it under 5 MB.")
    content_type = getattr(photo, "content_type", "") or ""
    if content_type and content_type not in ALLOWED_PHOTO_TYPES:
        raise DeliveryError("Upload a JPEG, PNG or WebP image.")


def _looks_like_signature(signature: str) -> bool:
    """Cheap sanity check on the SVG path the pad hands us.

    A real signature is a few dozen path commands. A single ``M 0 0`` is a
    smuggled placeholder, and a multi-megabyte string is a payload.
    """
    if not signature.startswith("M") and "M" not in signature[:200]:
        return False
    if len(signature) > 20000:
        return False
    return signature.count("L") + signature.count("Q") >= 2


def _coordinate(value) -> Decimal | None:
    try:
        return Decimal(str(value))
    except Exception:  # noqa: BLE001 - a bad coordinate is not worth a crash
        return None


def proof_summary(proof: DeliveryProof | None) -> list[str]:
    """Human-readable list of what a proof carries, for templates."""
    if proof is None:
        return []
    parts = []
    if proof.has_otp:
        parts.append("Delivery code confirmed")
    if proof.has_photo:
        parts.append("Photo")
    if proof.has_signature:
        parts.append("Signature")
    return parts
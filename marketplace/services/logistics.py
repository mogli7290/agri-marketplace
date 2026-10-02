"""Logistics planning: turn orders into a routed shipment.

Each order contributes two stops — a pickup at the farmer's location and a
delivery to the buyer. The stops are optimised with :mod:`marketplace.services.routing`
and persisted as a ``Shipment`` with ordered ``ShipmentStop`` rows.
"""

from __future__ import annotations

import logging
from decimal import Decimal, ROUND_HALF_UP

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from marketplace.models import DeliveryPartner, Order, Shipment, ShipmentStop
from marketplace.services import routing
from marketplace.services import ai as ai_service

logger = logging.getLogger(__name__)


class LogisticsError(Exception):
    """Raised when a shipment cannot be planned."""


def estimate_delivery_fee(distance_km: float) -> Decimal:
    """Base fee plus a per-kilometre component."""
    amount = Decimal(str(settings.DELIVERY_BASE_FEE)) + (
        Decimal(str(settings.DELIVERY_PER_KM_FEE)) * Decimal(str(distance_km))
    )
    return amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _profile_point(lat, lng) -> tuple[float, float] | None:
    if lat is None or lng is None:
        return None
    return (float(lat), float(lng))


@transaction.atomic
def plan_shipment(order_ids, partner: DeliveryPartner | None = None) -> Shipment:
    """Create an optimised shipment for the given order ids.

    Orders without coordinates are still added as stops but excluded from
    distance calculations.
    """
    orders = list(
        Order.objects.select_related("listing__farmer", "buyer").filter(pk__in=order_ids)
    )
    if not orders:
        raise LogisticsError("No orders found for the given ids.")

    shipment = Shipment.objects.create(partner=partner)
    stops_to_create: list[dict] = []

    for order in orders:
        farmer = order.listing.farmer
        buyer = order.buyer
        stops_to_create.append(
            {
                "order": order,
                "kind": "pickup",
                "label": f"Pickup: {farmer.full_name}, {farmer.village}",
                "latitude": farmer.latitude,
                "longitude": farmer.longitude,
            }
        )
        delivery_label = buyer.business_name or buyer.user.get_full_name() or buyer.phone_number
        stops_to_create.append(
            {
                "order": order,
                "kind": "delivery",
                "label": f"Delivery: {delivery_label}, {buyer.city}",
                "latitude": order.delivery_latitude or buyer.latitude,
                "longitude": order.delivery_longitude or buyer.longitude,
            }
        )

    # Persist stops first so we have ids to order.
    created = [
        ShipmentStop.objects.create(shipment=shipment, **stop) for stop in stops_to_create
    ]

    # Determine the origin: partner's live position, else the first pickup.
    origin = None
    if partner is not None:
        origin = _profile_point(partner.current_latitude, partner.current_longitude)
    if origin is None:
        first_pickup = next((s for s in created if s.kind == "pickup"), None)
        origin = _profile_point(
            first_pickup.latitude if first_pickup else None,
            first_pickup.longitude if first_pickup else None,
        )
    if origin is None:
        origin = (0.0, 0.0)

    geo_stops = [
        (stop.id, _profile_point(stop.latitude, stop.longitude))
        for stop in created
    ]
    geo_stops = [(sid, pt) for sid, pt in geo_stops if pt is not None]

    notes = "No coordinates available; stops kept in creation order."
    if geo_stops:
        baseline = routing.optimize_stops(origin, geo_stops)
        best_order = baseline["order"]
        best_distance = baseline["distance_km"]
        method = "geometric"

        # AI proposes; we verify and only accept a genuinely shorter route.
        geo_ids = {sid for sid, _ in geo_stops}
        ai_stops = [
            {
                "id": stop.id,
                "label": stop.label,
                "lat": float(stop.latitude),
                "lng": float(stop.longitude),
            }
            for stop in created
            if stop.id in geo_ids
        ]
        proposal = ai_service.suggest_route(ai_stops, origin=origin)
        if proposal:
            points = dict(geo_stops)
            candidate = [origin] + [points[sid] for sid in proposal["order"]]
            candidate_distance = round(routing.path_distance(candidate), 2)
            if candidate_distance < best_distance:
                best_order = proposal["order"]
                best_distance = candidate_distance
                method = "llm-verified"

        order_index = {sid: i for i, sid in enumerate(best_order)}
        for stop in created:
            if stop.id in order_index:
                stop.sequence = order_index[stop.id]
                stop.save(update_fields=["sequence"])
        shipment.planned_distance_km = Decimal(str(best_distance))
        shipment.planned_cost = estimate_delivery_fee(best_distance)
        notes = f"Route optimised across {len(geo_stops)} stops over {best_distance} km ({method})."

    shipment.optimized_at = timezone.now()
    shipment.route_notes = notes
    shipment.save(update_fields=["planned_distance_km", "planned_cost", "optimized_at", "route_notes"])

    # Distribute the delivery fee evenly across the shipment's orders.
    if orders:
        share = (shipment.planned_cost / len(orders)).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )
        for order in orders:
            order.delivery_fee = share
            order.pickup_hub = order.listing.farmer.village
            order.save(update_fields=["delivery_fee", "pickup_hub", "updated_at"])

    logger.info("Planned shipment %s for %s orders", shipment.pk, len(orders))
    return shipment

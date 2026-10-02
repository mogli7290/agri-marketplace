"""High-level demand forecasting.

Bridges the persisted marketplace data (historical orders and listings) with
the AI service, and stores the result as a :class:`DemandForecast`.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from decimal import Decimal

from django.db.models import Sum
from django.utils import timezone

from marketplace.models import Crop, DemandForecast, Order
from marketplace.services import ai as ai_service

logger = logging.getLogger(__name__)


def weekly_demand_history(crop: Crop, region: str = "", weeks: int = 8) -> list[float]:
    """Return total ordered quantity per week for the last ``weeks`` weeks."""
    since = timezone.now() - timedelta(weeks=weeks)
    qs = Order.objects.filter(
        listing__crop=crop,
        created_at__gte=since,
    ).exclude(status="cancelled")
    if region:
        qs = qs.filter(listing__farmer__district__iexact=region)

    buckets = [Decimal("0")] * weeks
    for created_at, quantity in qs.values_list("created_at", "quantity_ordered"):
        weeks_ago = int((timezone.now() - created_at).days // 7)
        index = weeks - 1 - weeks_ago
        if 0 <= index < weeks:
            buckets[index] += quantity
    return [float(x) for x in buckets]


def reference_price(crop: Crop, region: str = "") -> Decimal | None:
    """Median-ish asking price currently listed for a crop in a region."""
    qs = Order.objects.filter(listing__crop=crop).exclude(agreed_price_per_unit=0)
    if region:
        qs = qs.filter(listing__farmer__district__iexact=region)
    aggregate = qs.aggregate(total=Sum("agreed_price_per_unit"))
    count = qs.count()
    if aggregate["total"] and count:
        return (aggregate["total"] / count).quantize(Decimal("0.01"))
    # Fall back to any active listing price.
    from marketplace.models import Listing

    listing = (
        Listing.objects.filter(crop=crop, status="active")
        .order_by("-created_at")
        .values_list("price_per_unit", flat=True)
        .first()
    )
    return listing


def forecast_for_crop(
    crop: Crop, region: str = "", horizon_days: int = 7, persist: bool = True
) -> dict:
    """Produce (and by default store) a forecast for a crop."""
    history = weekly_demand_history(crop, region)
    base_price = reference_price(crop, region)
    result = ai_service.forecast_demand(
        crop_name=crop.name,
        region=region or "unspecified",
        horizon_days=horizon_days,
        history=history,
        base_price=base_price,
        unit=crop.unit,
    )
    if persist:
        DemandForecast.objects.create(
            crop=crop,
            region=region or "unspecified",
            horizon_days=horizon_days,
            predicted_quantity=result.get("predicted_quantity"),
            predicted_price_per_unit=result.get("predicted_price_per_unit"),
            confidence=result.get("confidence") or 0,
            method=result.get("method", "heuristic"),
            rationale=result.get("rationale", ""),
            raw_response=result.get("raw_response"),
        )
    result["crop"] = crop.name
    result["region"] = region or "unspecified"
    result["history"] = history
    return result

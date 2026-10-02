"""AI-backed demand forecasting and price suggestions.

The provider is any OpenAI-compatible chat completions endpoint (SambaNova
Cloud by default; OpenAI, Together, Groq, etc. also work by setting
``AI_BASE_URL``). When no API key is configured — or the provider errors — the
module degrades gracefully to deterministic heuristics so the marketplace keeps
working offline and in tests.
"""

from __future__ import annotations

import json
import logging
import re
from decimal import Decimal, ROUND_HALF_UP
from statistics import mean
from typing import Any, Iterable

from django.conf import settings

logger = logging.getLogger(__name__)

_JSON_BLOCK = re.compile(r"\{.*\}", re.DOTALL)


def is_configured() -> bool:
    return bool(getattr(settings, "AI_API_KEY", ""))


def _client():
    """Build a lazily-instantiated OpenAI-compatible client."""
    from openai import OpenAI

    return OpenAI(
        base_url=settings.AI_BASE_URL,
        api_key=settings.AI_API_KEY,
        timeout=settings.AI_TIMEOUT_SECONDS,
    )


def _chat_json(system_prompt: str, user_prompt: str) -> dict[str, Any]:
    """Ask the model for a JSON object and parse it defensively.

    Raises ``RuntimeError`` if the provider is unconfigured or unparseable so
    callers can fall back to heuristics.
    """
    if not is_configured():
        raise RuntimeError("AI provider is not configured")

    client = _client()
    response = client.chat.completions.create(
        model=settings.AI_MODEL,
        temperature=0.2,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
    )
    content = response.choices[0].message.content or ""
    match = _JSON_BLOCK.search(content)
    if not match:
        raise RuntimeError("AI provider did not return JSON")
    return json.loads(match.group(0))


def _to_decimal(value: Any) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        return Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    except (ArithmeticError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Demand forecasting
# ---------------------------------------------------------------------------
def _heuristic_forecast(history: list[float], base_price: Decimal | None, horizon_days: int) -> dict:
    """Simple trend-aware fallback used when the LLM is unavailable."""
    if history:
        recent = history[-4:]
        average = mean(recent)
        # Compare the latest window against the previous one for a light trend.
        trend = 1.0
        if len(history) >= 4:
            older = mean(history[-8:-4]) or mean(history)
            if older:
                trend = max(0.5, min(1.5, mean(recent) / older))
        predicted_qty = average * (horizon_days / 7) * trend
        confidence = 55
        rationale = "Moving-average of recent order volumes with a short-term trend adjustment."
    else:
        predicted_qty = 0.0
        confidence = 35
        rationale = "No historical sales yet; returning a neutral baseline."

    predicted_price = base_price
    if base_price is not None and history:
        # Scarcity signal: more demand volume nudges the price up.
        predicted_price = (base_price * Decimal(str(round(1 + min(0.25, predicted_qty / 1000), 2)))).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )

    return {
        "predicted_quantity": round(predicted_qty, 2) if predicted_qty else None,
        "predicted_price_per_unit": predicted_price,
        "confidence": confidence,
        "method": "heuristic",
        "rationale": rationale,
        "raw_response": None,
    }


def forecast_demand(
    crop_name: str,
    region: str,
    horizon_days: int = 7,
    history: Iterable[float] | None = None,
    base_price: Decimal | None = None,
    unit: str = "kg",
) -> dict:
    """Forecast demand and price for a crop in a region.

    Returns a dict with ``predicted_quantity``, ``predicted_price_per_unit``,
    ``confidence``, ``method``, ``rationale`` and ``raw_response``.
    """
    history = [float(x) for x in (history or [])]
    system = (
        "You are an agricultural market analyst for an Indian farm-to-buyer "
        "marketplace. Respond with a single JSON object and no prose."
    )
    user = (
        f"Crop: {crop_name} (unit: {unit})\n"
        f"Region: {region}\n"
        f"Forecast horizon: {horizon_days} days\n"
        f"Recent weekly order volumes: {history or 'no history yet'}\n"
        f"Current average price per {unit}: {base_price if base_price is not None else 'unknown'}\n\n"
        "Return JSON with keys: predicted_quantity (number), "
        "predicted_price_per_unit (number), confidence (0-100 integer), "
        "rationale (short string)."
    )

    try:
        data = _chat_json(system, user)
        return {
            "predicted_quantity": _to_decimal(data.get("predicted_quantity")),
            "predicted_price_per_unit": _to_decimal(data.get("predicted_price_per_unit")),
            "confidence": int(data.get("confidence") or 0),
            "method": "llm",
            "rationale": str(data.get("rationale", ""))[:1000],
            "raw_response": data,
        }
    except Exception as exc:  # noqa: BLE001 - fall back on any provider failure
        logger.warning("AI forecast fell back to heuristic: %s", exc)
        return _heuristic_forecast(history, base_price, horizon_days)


# ---------------------------------------------------------------------------
# Price suggestion
# ---------------------------------------------------------------------------
_QUALITY_MULTIPLIER = {"A": Decimal("1.00"), "B": Decimal("0.90"), "C": Decimal("0.78")}


def suggest_route(stops: list[dict], origin: tuple[float, float] | None = None) -> dict | None:
    """Ask the model to propose a visiting order for route stops.

    ``stops`` items are dicts with ``id``, ``label`` and ``lat``/``lng``. The
    proposal is advisory only — callers must validate it (see
    ``marketplace.services.logistics``) because LLMs are not reliable
    optimisers. Returns ``None`` when the provider is unavailable.
    """
    if not is_configured() or not stops:
        return None
    system = (
        "You are a logistics planner. Given stops with coordinates, order them "
        "to minimise driving distance. Respond with JSON only."
    )
    lines = [f"Start at {origin}" if origin else "Start point unspecified"]
    for stop in stops:
        lines.append(f"id={stop['id']} label={stop['label']} lat={stop.get('lat')} lng={stop.get('lng')}")
    user = "\n".join(lines) + '\n\nReturn JSON: {"order": [ids in visit order], "rationale": "..."}'
    try:
        data = _chat_json(system, user)
        order = [int(x) for x in data.get("order", [])]
        valid_ids = {stop["id"] for stop in stops}
        if set(order) != valid_ids or len(order) != len(valid_ids):
            return None
        return {"order": order, "rationale": str(data.get("rationale", ""))[:500]}
    except Exception as exc:  # noqa: BLE001
        logger.warning("AI route suggestion skipped: %s", exc)
        return None


def suggest_price(
    crop_name: str,
    quality_grade: str,
    region: str,
    base_price: Decimal | None = None,
    unit: str = "kg",
) -> dict:
    """Suggest a fair asking price for a listing.

    Returns ``price`` (Decimal), ``method`` and ``rationale``.
    """
    system = (
        "You are a pricing advisor for farmers in India. Suggest a fair, "
        "competitive price that keeps the farmer profitable. Respond with JSON only."
    )
    user = (
        f"Crop: {crop_name} (unit: {unit})\nQuality grade: {quality_grade}\n"
        f"Region: {region}\nMarket reference price per {unit}: "
        f"{base_price if base_price is not None else 'unknown'}\n\n"
        "Return JSON with keys: price (number), rationale (short string)."
    )
    try:
        data = _chat_json(system, user)
        price = _to_decimal(data.get("price"))
        if price is None:
            raise RuntimeError("missing price")
        return {"price": price, "method": "llm", "rationale": str(data.get("rationale", ""))[:500]}
    except Exception as exc:  # noqa: BLE001
        logger.warning("AI price suggestion fell back to heuristic: %s", exc)
        if base_price is None:
            return {"price": None, "method": "heuristic", "rationale": "No reference price available."}
        multiplier = _QUALITY_MULTIPLIER.get(quality_grade, Decimal("1.00"))
        return {
            "price": (base_price * multiplier).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP),
            "method": "heuristic",
            "rationale": f"Reference price adjusted for grade {quality_grade}.",
        }

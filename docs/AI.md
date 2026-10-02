# AI in AgriMarket — how it works and how to use it

This note explains where AI is used, how to switch it on, how the code is
structured, and how to add your own AI features.

---

## 1. Where AI is used

There are **three** AI-powered capabilities, all in
`marketplace/services/ai.py`:

| Capability | Function | What it does |
| --- | --- | --- |
| **Demand forecasting** | `forecast_demand(...)` | Predicts how much of a crop will be demanded over the next N days and a fair price |
| **Price suggestion** | `suggest_price(...)` | Recommends an asking price for a listing by crop + quality grade |
| **Route proposal** | `suggest_route(...)` | Proposes a visiting order for delivery stops (verified, see below) |

They are exposed to users through:

| Surface | URL | Who |
| --- | --- | --- |
| Web forecast page | `/forecast/` and AJAX `/forecast/request/` | Farmers |
| Web price hint | `/listings/price-hint/` (button on the listing form) | Farmers |
| API price suggestion | `POST /api/listings/{id}/suggest_price/` | API clients |
| API listing forecast | `POST /api/listings/{id}/forecast/` | API clients |
| API generic forecast | `POST /api/forecasts/generate/` | API clients |
| Logistics | used inside `services/logistics.py` when planning a shipment | System |

---

## 2. Turning AI on

The provider is any **OpenAI-compatible chat completions endpoint**. The default
is **SambaNova Cloud**, which has a free tier.

1. Create an account at <https://cloud.sambanova.ai> and copy an API key.
2. Set these in `.env` (or your host's environment):

```env
AI_API_KEY=your-key-here
AI_BASE_URL=https://api.sambanova.ai/v1
AI_MODEL=Meta-Llama-3.1-8B-Instruct
AI_TIMEOUT_SECONDS=20
THROTTLE_AI=30/hour
```

3. Restart the app. `services/ai.py` picks the key up automatically.

**Leave `AI_API_KEY` blank and everything still works** — the app falls back to
deterministic heuristics (see §5). This is deliberate, so development, tests and
free deployments never depend on a paid or flaky service.

### Using a different provider

Any OpenAI-compatible API works — only change the two values:

| Provider | `AI_BASE_URL` | Example model |
| --- | --- | --- |
| SambaNova (free tier) | `https://api.sambanova.ai/v1` | `Meta-Llama-3.1-8B-Instruct` |
| OpenAI | `https://api.openai.com/v1` | `gpt-4o-mini` |
| Groq | `https://api.groq.com/openai/v1` | `llama-3.1-8b-instant` |
| Together | `https://api.together.xyz/v1` | `meta-llama/Llama-3.1-8B-Instruct-Turbo` |
| Local (Ollama/vLLM) | `http://localhost:11434/v1` | `llama3.1` |

---

## 3. How the code works

### The client (`services/ai.py`)

```python
def is_configured() -> bool:
    return bool(getattr(settings, "AI_API_KEY", ""))

def _client():
    from openai import OpenAI
    return OpenAI(
        base_url=settings.AI_BASE_URL,
        api_key=settings.AI_API_KEY,
        timeout=settings.AI_TIMEOUT_SECONDS,
    )

def _chat_json(system_prompt, user_prompt) -> dict:
    if not is_configured():
        raise RuntimeError("AI provider is not configured")
    response = _client().chat.completions.create(
        model=settings.AI_MODEL,
        temperature=0.2,
        messages=[{"role": "system", "content": system_prompt},
                  {"role": "user", "content": user_prompt}],
    )
    match = _JSON_BLOCK.search(response.choices[0].message.content or "")
    if not match:
        raise RuntimeError("AI provider did not return JSON")
    return json.loads(match.group(0))
```

`_chat_json` is the single choke point. It forces the model to answer with a
JSON object (parsed defensively with a regex) and **raises on any failure** —
which is what lets callers fall back cleanly.

### Forecasting (`services/forecasting.py`)

`forecast_for_crop()` gathers real data, calls the AI, and stores the result:

```python
history = weekly_demand_history(crop, region)     # last 8 weeks of order volume
base_price = reference_price(crop, region)        # recent average price
result = ai_service.forecast_demand(crop.name, region, horizon, history, base_price, crop.unit)
DemandForecast.objects.create(crop=crop, region=region, **result)   # persisted
```

So forecasts are not throwaway — every run is saved in `DemandForecast` and
listed on the `/forecast/` page.

### Pricing

```python
result = ai.suggest_price(crop_name="Tomato", quality_grade="A",
                          region="Pune", base_price=Decimal("40"))
# -> {"price": Decimal("46.00"), "method": "llm", "rationale": "..."}
```

The API's `suggest_price` action writes the result onto
`Listing.ai_suggested_price` so the farmer sees it later.

### Route optimisation — AI proposes, geometry disposes

LLMs are poor optimisers, so the LLM is only allowed to *suggest*. The
geometric solver (`services/routing.py`: haversine + nearest-neighbour + 2-opt)
produces a baseline, and the AI's proposal is accepted **only if it measures
strictly shorter**:

```python
baseline = routing.optimize_stops(origin, geo_stops)
best_order, best_distance, method = baseline["order"], baseline["distance_km"], "geometric"

proposal = ai_service.suggest_route(ai_stops, origin=origin)
if proposal:
    candidate = [origin] + [points[sid] for sid in proposal["order"]]
    candidate_distance = round(routing.path_distance(candidate), 2)
    if candidate_distance < best_distance:
        best_order, best_distance, method = proposal["order"], candidate_distance, "llm-verified"
```

This is the pattern to reuse for any AI output that affects money or operations:
**generate → verify → only apply if it passes the check.**

---

## 4. Cost and safety controls

- **Fallback:** no key → heuristics. No feature is ever hard-broken by AI.
- **Timeout:** `AI_TIMEOUT_SECONDS` (default 20s) stops slow providers from
  hanging a request.
- **Throttling:** AI endpoints use a dedicated DRF throttle (`AIScopedThrottle`,
  scope `ai`, default `30/hour`) to prevent runaway API spend.
- **Advisory only:** AI never mutates stock or money directly. The worst case of
  a bad model response is a rejected proposal, not a bad transaction.

---

## 5. The offline fallback (why tests pass without a key)

When the provider is missing or errors, `_heuristic_forecast()` runs:

- Uses a **moving average** of recent weekly volumes.
- Applies a light **trend factor** (latest window vs previous).
- Adjusts price by a **scarcity signal** based on predicted volume.
- Reports `method="heuristic"` and a modest confidence score, so the UI is
  honest about where a number came from.

Price suggestion falls back to a **grade multiplier** (`A=1.00, B=0.90,
C=0.78`) applied to the reference price.

Because of this, `python manage.py test marketplace` passes with **no API key
and no network**.

---

## 6. How to add your own AI feature

1. **Add a function in `services/ai.py`** that calls `_chat_json(...)` and does
   its own heuristic fallback in a `try/except Exception`.
2. **Return a dict** with at least a `method` field (`"llm"` or `"heuristic"`)
   and a `rationale`, so the UI can show provenance.
3. **Expose it** with a thin view or a DRF `@action(detail=..., methods=["post"],
   throttle_classes=[AIScopedThrottle])`.
4. **Persist it** if it is worth keeping (like `DemandForecast`) so it can be
   reviewed and used as future training data.
5. **Add a test** that patches `AI_API_KEY=""` and asserts the heuristic path —
   this keeps CI free and offline.

Example skeleton:

```python
def recommend_crops(district: str, season: str) -> dict:
    system = "You advise farmers. Reply with JSON only."
    user = f"District: {district}. Season: {season}. Suggest 3 crops."
    try:
        data = _chat_json(system, user)
        return {"crops": data.get("crops", []), "method": "llm", "rationale": str(data)}
    except Exception as exc:
        logger.warning("crop recommendation fell back: %s", exc)
        return {"crops": [], "method": "heuristic", "rationale": "Provider unavailable."}
```

### Ideas for the next AI features

- **Spoilage / shelf-life estimate** per crop to prioritise delivery order.
- **Buyer demand matching** — recommend listings to a buyer from order history.
- **Anomaly flagging** on suspicious price or quantity inputs.
- **Voice/vernacular input** in the farmer's preferred language (the
  `FarmerProfile.preferred_language` field is already there).
- **Image quality check** on listing photos (a vision model) before publishing.

---

## 7. Testing the AI quickly

From the Django shell, with a key configured:

```bash
python manage.py shell -c "
from marketplace.services import ai, forecasting
from marketplace.models import Crop
print(ai.suggest_price('Tomato','A','Pune', base_price=40))
print(forecasting.forecast_for_crop(Crop.objects.first(), 'Pune'))
"
```

Without a key, the same commands return `method: heuristic` — proving the
fallback works.

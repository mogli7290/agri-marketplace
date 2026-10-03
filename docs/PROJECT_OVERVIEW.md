# AgriMarket — Project Overview

## What this project is

AgriMarket is a **digital marketplace that connects farmers and FPOs directly
with consumers and bulk buyers**. It removes middlemen, provides logistics
support, and uses AI to forecast demand and optimise delivery routes.

**The three goals it is built around:**

1. **Better prices for farmers** — they list produce at their own price and keep
   the full sale value (`farmer_payout` = quantity × agreed price; the platform
   charges the *buyer* a small fee, not the farmer).
2. **Lower prices for buyers** — buyers source straight from the producer.
3. **Less supply-chain waste** — demand forecasts guide what to grow/list, and
   multi-stop route optimisation reduces transport cost and spoilage.

Built with **Django 5.2 + Django REST Framework**, **PostgreSQL** (SQLite for
local dev), **Redis** (optional cache), **Razorpay** (UPI payments) and any
**OpenAI-compatible LLM** (SambaNova Cloud by default).

---

## Who does what

| Actor | Can do |
| --- | --- |
| **Farmer / FPO** | Register, publish listings (crop, quantity, grade, price, photo), edit/cancel them, get AI price suggestions and demand forecasts, receive and manage orders, see revenue |
| **Consumer / Bulk buyer** | Register, browse/search/filter listings, place orders (atomic stock reservation), track order status, pay online via UPI/card |
| **Delivery partner / staff** | Plan optimised multi-stop shipments, view routes and stops, manage operations in the admin |
| **API clients** | Register, authenticate with JWT, and use the full marketplace over REST |

---

## Feature summary

| Area | Implemented |
| --- | --- |
| Authentication | Farmer & buyer roles, email-or-username login, profile view/edit, JWT for the API |
| Marketplace | Listings with photo, quality grade, organic flag, harvest date; search, crop/district/price filters, pagination. A listing with no photo falls back to artwork chosen from the crop name (`Crop.placeholder_image`) |
| Negotiation | Buyers propose their own price; farmers accept or reject. Accepting creates the order at the agreed price (with a fresh stock check) |
| Orders | Atomic stock reservation, platform fee, delivery fee, status timeline with audit trail |
| Payments | Razorpay checkout (UPI/cards/netbanking), checkout signature verification, signed webhooks |
| Logistics | Delivery partners, multi-order shipments, pickup+delivery stops, optimised routes, distance-based delivery fees |
| AI | Demand forecasting, price suggestions, route proposals (all with a working offline fallback) |
| API | JWT-secured REST API, filtering, search, ordering, pagination, throttling |
| Ops | Health check, admin, logging, Docker, Postgres, WhiteNoise static serving, hardened settings |

---

## Architecture

```
config/                     project settings, URLs, WSGI/ASGI, /healthz
marketplace/
  models.py                 domain models (participants, supply, demand, logistics, intelligence)
  views.py                  server-rendered views (auth, listings, orders, payments, logistics, forecasts)
  forms.py                  validated forms
  auth_backends.py          email-or-username authentication
  context_processors.py     shared template context
  admin.py                  back-office configuration
  services/                 business logic (the "brain")
    ai.py                   LLM client + heuristics (forecast, price, route)
    forecasting.py          builds history, stores DemandForecast rows
    routing.py              haversine, nearest-neighbour + 2-opt route optimisation
    logistics.py            turns orders into a routed Shipment
    orders.py               order lifecycle + fees (shared by web and API)
    payments.py             Razorpay order creation + HMAC verification
  api/                      DRF serializers, viewsets, permissions, pagination
  tests/                    services, views, API, page smoke tests
templates/                  Bootstrap 5 UI
static/                     site CSS + JS
Dockerfile, docker-compose.yml, gunicorn.conf.py, nginx.conf, render.yaml, Procfile
docs/                       this documentation
```

**Design principles used throughout:**

- **Money and lifecycle rules live in one place.** `services/orders.py` owns fee
  calculation and status transitions; the web views, the API, and the payment
  webhook all call into it, so the rules can never drift apart.
- **AI is advisory and verified.** Route proposals from the LLM are accepted
  only if they actually shorten the geometric route; forecasts fall back to
  deterministic heuristics when no provider is configured. The app is never
  broken by a missing API key.
- **Configuration is environment-driven.** The same image runs locally and in
  production; secrets are never committed.

---

## Data model

| Model | Purpose |
| --- | --- |
| `FarmerProfile` | A farmer or FPO (name, phone, village/district/state, geo point, KYC flag) |
| `BuyerProfile` | A consumer or bulk buyer (type, business name, city, address, geo point) |
| `Crop` | A crop type with its unit (kg, quintal, dozen) |
| `Listing` | A farmer's offer to sell a quantity of a crop, with price and grade |
| `Order` | A buyer's order against one listing (quantity, agreed price, fees, status, payment status) |
| `OrderStatusHistory` | Immutable audit trail of every status change |
| `Payment` | A payment attempt (Razorpay order/payment ids, amount, status, method) |
| `DeliveryPartner` | A logistics provider (vehicle, capacity, availability, live location) |
| `Shipment` | A planned movement of one or more orders with an optimised route |
| `ShipmentStop` | An ordered pickup/delivery stop on a shipment |
| `DemandForecast` | A stored demand/price prediction (LLM or heuristic) |
| `DemandRequest` / `RequestOffer` | The demand board: a buyer posts a need, farmers respond |
| `Conversation` / `Message` | The thread attached to a request or an order |
| `PayoutMethod` | Where a seller's (or a transporter's) money is sent |
| `Payment` (direct UPI) | `upi_direct` and `upi_to_farmer` claims, settled by reconciliation |
| `Reconciliation` | A bank CSV matched against pending UPI claims |
| `Payout` / `PayoutItem` | A weekly settlement statement for a farmer or a delivery partner |
| `EmailVerification` | Signed, hashed, single-use address confirmation |

**Order lifecycle:** `placed → confirmed → picked_up → in_transit → delivered →
paid` (with `cancelled` allowed from `placed` or `confirmed`). Transitions are
validated centrally, so an illegal jump is rejected everywhere.

---

## What was already here vs. what was done

### Already present (partial foundation)

- Django project skeleton with `FarmerProfile`, `BuyerProfile`, `Crop`,
  `Listing`, `Order`, `OrderStatusHistory`.
- Basic session auth (login/register) and two templates.
- A DRF API skeleton and an admin file.

### Fixed (the foundation did not run)

| Problem | Fix |
| --- | --- |
| No `wsgi.py` / `asgi.py` — the app could not start under a server | Added both |
| `marketplace/migrations/` was empty — no schema existed | Created `0001_initial` |
| Views referenced templates that did not exist | Built every missing template |
| `marketplace/utils/ai.py` was imported but missing | Replaced with a real `services/` layer |
| Order-creation flow was absent entirely | Added web + API order creation |
| `OrderDetailView` filtered on a non-existent field (`farmer__user`) | Corrected to `listing__farmer` |
| Dashboards crashed for the wrong role | Added role guards |
| Login form ignored email despite the placeholder | Added an email-or-username backend |
| Hardcoded `SECRET_KEY`, `DEBUG=True`, `ALLOWED_HOSTS=['*']`, SQLite only | Environment-driven, hardened settings |
| Committed `db.sqlite3` and `__pycache__` | Removed and gitignored |
| Registration skipped validation of role fields | Real validation for farmer vs buyer fields |

### Added (the actual product)

- **Negotiation**: buyers send a price offer; farmers approve it — and only then
  does an order exist, at the agreed price. Stock is re-checked at acceptance.
- **Logistics**: delivery partners, shipments, stops, route optimisation,
  distance-based delivery fees.
- **AI**: demand forecasting, price suggestions, route proposals, with offline
  fallback and persisted results.
- **Payments**: Razorpay checkout + signature + webhook handling.
- **API**: JWT auth (`register`, `token`, `me`), role-scoped viewsets, actions
  for pricing/forecasting/payment/shipment planning, filters, throttling.
- **Ops**: `/healthz`, Docker, docker-compose, gunicorn, nginx, Render blueprint,
  Procfile, seed command, 473 automated tests, and this documentation.

---

## Running it locally

```bash
pip install -r requirements.txt
cp .env.example .env          # then edit values
python manage.py migrate
python manage.py seed_data --demo
python manage.py runserver
```

Open <http://127.0.0.1:8000>. Demo logins (after `seed_data --demo`):
`demo_farmer` / `demo_buyer`, password `DemoPass123`. Create an admin with
`python manage.py createsuperuser`.

## Verifying it

```bash
python manage.py check              # config sanity
python manage.py check --deploy     # production hardening (run with DJANGO_DEBUG=false)
python manage.py test marketplace   # 473 tests
```

---

## Current status & honest limitations

- The app is complete and tested against SQLite locally; the production path
  (gunicorn + WhiteNoise + Postgres) is verified to serve correctly.
- **AI and payments are optional at runtime.** Without keys, forecasts/pricing
  use heuristics and the pay button is hidden — nothing breaks.
- **Listings are single-lot.** An order draws down one listing's stock; there is
  no partial-shipment or multi-listing cart yet.
- **AI history is shallow** — forecasts use recent order volume as the signal.
  With real traffic, more history improves them.
- **No background job runner** — AI calls, notifications and reconciliation
  happen in-request (with timeouts and fallbacks). Add Celery/RQ later if you want
  them off the request path.
- **Money moves by hand.** The payout ledger produces statements; a human
  transfers the funds and records the reference. `mark_paid()` is the only seam a
  real payout API needs.
- **Uploaded photos are not persistent on free hosts** — configure S3
  (`USE_S3=true`) for durable media.

## Documentation

| Doc | Covers |
| --- | --- |
| `docs/AI.md` | Price hints, forecasting, routing optimisation, fallbacks |
| `docs/PAYMENTS.md` | Razorpay, direct UPI, refunds, reconciliation |
| `docs/PAYOUTS.md` | The settlement ledger for farmers and delivery partners |
| `docs/DEMAND_BOARD.md` | Requests, offers, contact gating, direct deals |
| `docs/NOTIFICATIONS.md` | Every automated email, when it fires, and why |
| `docs/LOGISTICS.md` | Shipment planning, routing, capacity |
| `docs/PROOF_OF_DELIVERY.md` | Delivery codes, signatures, disputes, and how they gate payouts |
| `docs/ROLES.md` | Who can do what |
| `docs/DEPLOYMENT.md` | Render, Docker, nginx, free-tier limits |

See `docs/AI.md` for the AI details and `docs/DEPLOYMENT.md` to ship it.

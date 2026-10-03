# Roles & permissions — what each user can do

Two marketplace roles (Farmer/FPO, Buyer) plus staff/admin. Registration picks
the role; it decides what you see, what you can act on, and what is blocked.

---

## 1. Seller — Farmer / FPO

### Powers

| Capability | Where | Notes |
| --- | --- | --- |
| Register as a farmer/FPO | `/register/` | Name, phone, village, district, state, optional FPO flag |
| **Dashboard** | `/dashboard/farmer/` | Active listings, orders received, settled revenue; listings + orders tables |
| **Publish a listing** | `/listings/create/` | Crop, quantity, quality grade (A/B/C), price, harvest date, organic flag, description, photo |
| **AI price suggestion** | *Suggest price* button on the listing form | Returns a suggested ₹ price, method (llm/heuristic), and a rationale |
| **Edit own listing** | `/listings/<pk>/edit/` | Owner only, and only while still active |
| **Cancel own listing** | `/listings/<pk>/cancel/` | Owner only (removes it from public browse) |
| **Demand forecasting** | `/forecast/` | Pick crop + region + horizon (1–365 days) → predicted demand, suggested price, confidence, rationale; every run is saved to history |
| **See orders received** | `/dashboard/farmer/` → `/orders/<pk>/` | Buyer, crop, quantity, status, payment state; full status timeline and totals |
| **Review price offers** | `/offers/`, or directly on the listing | Buyers propose a price; **Accept** creates the order at *their* price, **Reject** with an optional note. Incoming offers also appear on the listing page |
| **Move an order forward** | order page | Mark *picked up* → *in transit* → *delivered* (valid transitions only) |
| **See their shipments** | `/shipments/` | Only shipments containing their own orders — stops, route, km, cost |
| **Profile** | `/profile/`, `/profile/edit/` | Includes pickup coordinates used by route planning |

### API parity (JWT `Bearer` token)

`POST/PUT/PATCH/DELETE` on `/api/listings/` (owner only) ·
`POST /api/listings/{id}/suggest_price/` · `POST /api/listings/{id}/forecast/` ·
list and manage own `/api/orders/` · `POST /api/orders/{id}/advance_status/`.

---

## 2. Buyer — Consumer / Bulk buyer

### Powers

| Capability | Where | Notes |
| --- | --- | --- |
| Register as consumer or bulk buyer | `/register/` | Business name, city, address; bulk buyers get more volume |
| **Dashboard** | `/dashboard/buyer/` | Total orders, awaiting payment, total spend; order history |
| **Browse & search** | `/` | Text search (crop / farmer / village), crop filter, district filter, price min–max, organic-only, pagination |
| **Listing detail** | `/listings/<pk>/` | Farmer identity and location, AI-suggested price if set, related listings |
| **Place an order** | `/listings/<pk>/order/` | Quantity only — **the price is the farmer's listed price**; stock is reserved atomically |
| **Propose your own price** | listing page → *Or propose your own price* | Sends an offer (quantity + price + note) to the farmer; an order is created **only if they accept** |
| **Track offers** | `/offers/` | Pending / accepted / rejected / withdrawn, with the resulting order link |
| **Confirm an order** | order page | Starts the lifecycle: placed → confirmed |
| **Cancel an order** | order page | Buyer (or staff) only, and only while *placed* or *confirmed* |
| **Pay online (UPI/cards)** | order page | Razorpay checkout when keys are configured; amount is computed **server-side** from the order |
| **Track progress** | `/orders/<pk>/` | Full status timeline, payment state, and the shipment carrying it |
| **Profile** | `/profile/`, `/profile/edit/` | Delivery address and delivery coordinates (used for routing and fee) |

### API parity (JWT `Bearer` token)

`POST /api/orders/` · `POST /api/orders/{id}/advance_status/` ·
`POST /api/orders/{id}/cancel/` · `POST /api/orders/{id}/initiate_payment/` ·
`POST /api/orders/{id}/verify_payment/` · list own `/api/orders/`.

---

## 3. Any signed-in user

- `/forecast/` and `/forecast/request/` (the nav link shows for farmers, but the
  endpoint is login-gated, not role-gated).
- `/listings/price-hint/` (same — login-gated).
- `/api/auth/me/`, profile pages.
- Browse listings anonymously (read-only).

---

## 4. Staff / admin

| Capability | Where |
| --- | --- |
| **Plan shipments** (bundle orders into an optimised multi-stop route) | `/shipments/` or `POST /api/shipments/plan/` |
| See **all** shipments and orders | `/shipments/`, admin |
| Manage **delivery partners** | admin, or `/api/partners/` (writes are admin-only) |
| Verify farmer KYC, manage users, crops, listings, orders, payments, forecasts | `/admin/` |
| Override any order transition / cancel on a buyer's behalf | order page |

Create one with `python manage.py createsuperuser`.

---

## 5. Guard rails (what each role is deliberately blocked from)

| Rule | Enforced in |
| --- | --- |
| **Buyers cannot set the order price directly** — the price comes from the listing, or from an offer the farmer accepted | `OrderForm` (no price field), `Order.agreed_price_per_unit` read-only on the API |
| **Only the seller (or staff) can accept/reject an offer** | `offers.respond_offer`, `PriceOfferViewSet.respond` |
| **Only the buyer (or staff) can withdraw a pending offer** | `offers.cancel_offer` |
| **One pending offer per buyer per listing** | DB `UniqueConstraint` + service check |
| **An offer reserves no stock** — stock is re-checked and taken only when the farmer accepts | `offers.accept` → `orders.create_order` |
| **Farmers cannot place orders** — you must be a buyer | `IsBuyer` (API), buyer-profile check (web) |
| **Nobody can order their own listing** | `OrderSerializer.validate`, `OrderCreateView` |
| **Buyers cannot create/edit listings** | `IsFarmer` (API), `UserPassesTestMixin` (web) |
| **You can only edit/cancel your own listings** | owner + active check |
| **You only ever see your own orders/shipments** | queryset scoping in `OrderViewSet`, `OrderDetailView`, `ShipmentListView` |
| **Illegal status jumps are rejected** (e.g. placed → delivered) | `Order.STATUS_TRANSITIONS`, validated in `orders.advance_status` |
| **Only the buyer (or staff) can cancel**, and only from placed/confirmed | `OrderCancelView` |
| **Only the buyer can initiate/verify payment** | `PaymentInitiateView`, `IsBuyer` |
| **Shipment planning is staff-only** | `PlanShipmentView`, `IsAdminUser` |
| **Amount is always computed server-side** from the order, never trusted from the browser | `PaymentInitiateView` |

All of these are covered by tests in `marketplace/tests/` (533 tests).

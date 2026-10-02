# Roadmap — what to work on next, in order

The app is complete and tested for its core flows. This is the prioritised,
step-by-step list of what still needs doing, grouped into phases. Do them in
order — each phase assumes the one before it.

**Effort key:** S = a day or less · M = a few days · L = a week or more.

---

## Phase 0 — Ship it (do this first)

Get it live so everything else is against a real deployment.

1. **Deploy to Render (free)** — follow `docs/DEPLOYMENT.md`. *(S)*
2. **Set production env** — `DJANGO_DEBUG=false`, a strong `DJANGO_SECRET_KEY`,
   `DJANGO_ALLOWED_HOSTS` = your domain. *(S)*
3. **Create the admin user** — via `DJANGO_SUPERUSER_*` or `createsuperuser`. *(S)*
4. **Move to a non-expiring free database** — Render's free Postgres expires
   after ~30 days; switch `DATABASE_URL` to Neon/Supabase. *(S)*
5. **Persistent media** — set `USE_S3=true` so listing photos survive
   redeploys. *(S)*
6. **Register the Razorpay webhook** at `/payments/webhook/` (if using payments). *(S)*
7. **Add your AI key** — optional; improves forecasts and pricing. *(S)*

**Done when:** the live site lets a real user register, list, order, and pay
(in test mode) end to end.

---

## Phase 1 — Transport hardening (the biggest functional gap)

See `docs/LOGISTICS.md` §7 for detail.

1. **Street-level distance** — replace haversine with a distance-matrix API
   (self-hosted OSRM/OpenRouteService/Google) so km and fees reflect real roads. *(M)*
2. **Geocoding** — convert profile addresses to coordinates at signup/admin
   (Nominatim/Mapbox/Google) instead of requiring manual lat/lng. *(M)*
3. **Live tracking** — a partner location-ping endpoint plus a buyer-facing
   tracking page with a map. *(M)*
4. **Enforce vehicle capacity** — refuse to bundle when the total load exceeds
   `capacity_kg`; split into multiple shipments. *(S)*
5. **Auto-assign partners** — match the nearest available partner that fits the
   load, with a manual override. *(S)*
6. **Per-order delivery fee** — allocate the shipment cost by each order's own
   leg distance instead of splitting evenly. *(S)*
7. **Shipment state machine** — validate transitions and stamp each stop's
   completion time. Dispatch/complete now exist as admin actions (they decide
   whether a partner is owed money); validating the transitions properly still
   needs doing. *(S)*
8. ~~**Proof of delivery**~~ — *done* (`PROOF_OF_DELIVERY.md`). A buyer code,
   signature or photo at the door, with a dispute path. What is still missing is
   photo **retention/redaction** and the buyer-side OTP prompt (which wants SMS).
9. **Partner portal** — dedicated UI for drivers to accept, start, and complete
   shipments. Proof capture exists on the shipment page; accepting and starting
   a run does not. *(M)*

**Done when:** an operator can plan a realistic route and a driver can execute
and prove it, with honest road distances and costs.

---

## Phase 2 — Payments completion

See `docs/PAYMENTS.md` §6 and `docs/PAYOUTS.md`.

1. ~~**Payouts to farmers/FPOs**~~ — *done as a ledger plus manual transfer*
   (`PAYOUTS.md`). Statements are drafted weekly for farmers **and** delivery
   partners, and `mark_paid` is the single seam where RazorpayX would slot in.
   What remains is automating the transfer itself. *(L)*
2. **Refunds** — a refund endpoint calling Razorpay's refund API, updating
   `payment_status`. *(M)*
3. **Invoices/receipts** — generate a PDF per order from the `Payment` rows. *(M)*
4. ~~**Idempotency keys**~~ on Razorpay order creation to avoid duplicates on retry. *(S)*

**Done when:** money actually reaches the farmer automatically, and a buyer can
be refunded.

---

## Phase 3 — Product gaps

1. **Cart / multi-listing orders** — one order across several farmers/lots. *(M)*
2. **Partial fulfilment & split shipments** — deliver part of an order when only
   some stock is ready. *(L)*
3. ~~**Notifications**~~ — *email is done* (`NOTIFICATIONS.md`) for order, offer,
   payment, dispatch, delivery, payout and delivery codes. **SMS/WhatsApp is
   deliberately out** — TRAI requires DLT registration (which needs a business
   PAN) and no provider has a permanent free tier. Evaluated and declined; the
   reasoning and the cheap mitigations are in `NOTIFICATIONS.md` §7. Revisit only
   if there is money for it. *(M)*
4. **Reviews & ratings** — buyers rate farmers; trust signals on listings. *(M)*
5. **Better search** — Postgres full-text search, synonyms, and sort by
   relevance/distance. *(S)*
6. **Reorder / favourites** — quick repeat buys for bulk buyers. *(S)*

**Done when:** a bulk buyer can run a realistic multi-item purchase loop.

---

## Phase 4 — AI upgrades

See `docs/AI.md` §6.

1. **Richer forecasting signal** — use longer history, seasonality, and weather;
   today it leans on recent order volume. *(M)*
2. **Move AI off the request path** — Celery/RQ background jobs so slow
   provider calls never block a page. *(M)*
3. **Constrained routing** — add time windows and capacity to the optimiser. *(M)*
4. **Buyer-side matching** — recommend listings to buyers from their history. *(M)*
5. **Vernacular & voice input** — use `FarmerProfile.preferred_language`. *(M)*
6. **Cache forecasts** — avoid regenerating identical forecasts. *(S)*

**Done when:** forecasts are measurably useful and never block a request.

---

## Phase 5 — Quality & operations

1. **CI** — GitHub Actions running `manage.py test` on every push. *(S)*
2. **Error tracking** — Sentry for backend errors and slow requests. *(S)*
3. **Scheduled database backups** — automated, with a tested restore. *(S)*
4. **More tests** — payment webhook, logistics edge cases, permission matrices;
   add coverage reporting. *(M)*
5. **Accessibility & i18n** — Hindi/regional languages, keyboard/contrast audit. *(M)*
6. ~~**Abuse controls**~~ — *partly done.* Rate limits on the public forms are in
   (`NOTIFICATIONS.md` §8): login, register, password reset, and verification
   resend are all capped per client address. Still open: CAPTCHA on register,
   and validation limits on the authenticated forms (offers, requests, chat).
   *(S)*
7. **Observability** — structured logs, request metrics, uptime alerting. *(M)*

**Done when:** you'd be comfortable letting strangers use it.

---

## Phase 6 — Scale (only when traffic demands it)

1. **Celery + Redis** for background work and scheduled jobs. *(M)*
2. **CDN** for static and media. *(S)*
3. **Connection pooling** (PgBouncer) and a read replica. *(M)*
4. **Caching layer** for hot reads (listings, forecasts). *(S)*

---

## The five things to do next, in order

If you only do five things:

1. **Deploy it** (Phase 0) — it's the fastest way to find real-world gaps.
2. **Switch to a non-expiring free Postgres** — otherwise the DB disappears in
   ~30 days.
3. **Add geocoding + street-level distance** (Phase 1.1–1.2) — makes delivery
   cost truthful.
4. ~~Add farmer payouts~~ — done, farmers *and* transporters
   (see [PAYOUTS.md](PAYOUTS.md)).
5. ~~Add notifications~~ — done (see [NOTIFICATIONS.md](NOTIFICATIONS.md)).

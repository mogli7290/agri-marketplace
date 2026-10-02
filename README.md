# AgriMarket — direct farm-to-buyer marketplace

AgriMarket connects farmers and FPOs directly with consumers and bulk buyers. It
adds logistics support (pickup/delivery routings) and AI-assisted demand
forecasting and pricing, with UPI settlement — either through a gateway
(Razorpay) or directly to a UPI ID with automatic bank-statement reconciliation.

**Why it matters**

- Better prices for farmers — no middlemen, transparent per-unit pricing.
- Lower prices for buyers — sourcing straight from the source.
- Less waste — demand forecasting and optimised delivery routes reduce
  supply-chain inefficiency.

Built with **Django 5.2**, **Django REST Framework**, **PostgreSQL**, **Redis**,
**Razorpay** (UPI) and an **OpenAI-compatible LLM** (SambaNova Cloud by default).

---

## Documentation

- **[Project overview](docs/PROJECT_OVERVIEW.md)** — what the project is, what was built, and its current status
- **[Roles & permissions](docs/ROLES.md)** — exactly what each user can do, and what they're blocked from
- **[AI guide](docs/AI.md)** — where AI is used, how to switch it on, and how to add your own AI features
- **[Demand board & fees](docs/DEMAND_BOARD.md)** — buyers posting needs, sellers responding, messaging, contact unlock, and exactly when the platform earns
- **[Payouts](docs/PAYOUTS.md)** — the settlement ledger for farmers *and* delivery partners, weekly runs, hold windows, and why nothing can ever be paid twice
- **[Notifications](docs/NOTIFICATIONS.md)** — every automated email, when it fires, and why it waits for the commit
- **[Proof of delivery](docs/PROOF_OF_DELIVERY.md)** — delivery codes, signatures and disputes, and how they gate the transporter's payout
- **[Transport & logistics](docs/LOGISTICS.md)** — shipments, route optimisation, delivery fees, and what's still missing
- **[Payments guide](docs/PAYMENTS.md)** — UPI the two ways (gateway or direct), how to enable either, and what's not built yet
- **[Roadmap](docs/ROADMAP.md)** — prioritised, step-by-step list of remaining work
- **[Deployment guide](docs/DEPLOYMENT.md)** — step-by-step for Render (free), Docker on a VPS, or any PaaS

---

## Feature overview

| Area | What's included |
| --- | --- |
| Accounts | Farmer/FPO and buyer roles, email-or-username login, email confirmation, profile management |
| Marketplace | Listings with photos, grades, organic flag, search & filters, pagination |
| Negotiation | Buyers propose their own price; farmers accept or reject — accepting creates the order at the agreed price |
| Demand board | Buyers post "I want 100 kg tomato"; sellers compete with priced offers; message threads; contact unlock on deal |
| Orders | Atomic stock reservation, platform fee, auditable status timeline |
| Payments | Razorpay checkout (UPI/cards/netbanking) **or** direct UPI with QR + bank-statement reconciliation; seller-chosen direct-to-farmer transfers; signature verification, webhooks, refunds |
| Logistics | Delivery partners, multi-order shipments, optimised multi-stop routes |
| Intelligence | LLM demand forecasts and price suggestions with offline fallback |
| API | JWT-secured REST API for all core resources |
| Ops | Health check, admin, Docker, Postgres, Redis, WhiteNoise, hardened settings |
| Settlement | Payout ledger with a weekly run, hold window, approval step, and bank reference trail |

---

## Architecture

```
config/                 project settings, URLs, WSGI/ASGI, health check
marketplace/
  models.py             domain models (participants, supply, demand, logistics, intelligence)
  views.py              server-rendered views
  forms.py              validated forms
  auth_backends.py      email-or-username authentication
  context_processors.py shared template context
  services/
    ai.py               LLM client + heuristic fallback (forecast, price, route)
    forecasting.py      builds history, persists DemandForecast
    routing.py          haversine, nearest-neighbour + 2-opt route optimisation
    logistics.py        turns orders into an routed Shipmentorders.py           order lifecycle + fees (shared by web and API)
    payments.py         Razorpay orders, capture/webhook settlement, refunds
    upi.py              direct UPI links, claims, confirmations, reversals
    reconciliation.py   matches a bank statement CSV to pending UPI claims
  api/                  DRF serializers, viewsets, permissions, pagination, JWT auth
  tests/                services, views, API, payments security, page smoke tests
templates/              Bootstrap 5 UI
static/                 site CSS and JS
Dockerfile, docker-compose.yml, gunicorn.conf.py, nginx.conf, render.yaml
```

### Design notes

- **AI is advisory, verified.** Route suggestions come from the geometric
  optimiser; an LLM proposal is only accepted if it actually shortens the
  route. Forecasts and price hints fall back to deterministic heuristics when
  no API key is set, so the app stays fully functional (and testable) offline.
- **Money math lives in one place.** `marketplace/services/orders.py` owns fee
  calculation and status transitions, and both the web views and the API call
  into it — so the rules can never drift apart.
- **Settings are environment-driven.** The same image runs locally and in
  production; secrets are never committed.

---

## Local setup

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env          # then edit values
python manage.py migrate
python manage.py seed_data --demo   # optional: crops + demo accounts
python manage.py runserver
```

Open http://127.0.0.1:8000.

To open the site from your phone (so emailed links work), bind to your LAN
address and point `SITE_URL` at it — `127.0.0.1` on a phone is the phone:

```bash
python manage.py runserver 0.0.0.0:8001
# .env: SITE_URL=http://<your-lan-ip>:8001   (find it with `hostname -I`)
# then: python manage.py send_test_email --to you@example.com --check-only
```

Demo logins (after `seed_data --demo`): `demo_farmer` / `demo_buyer`, password
`DemoPass123`. Create an admin with `python manage.py createsuperuser`.

Run the tests:

```bash
python manage.py test marketplace
```

### Environment variables

See `.env.example` for the full list. The important ones:

| Variable | Purpose |
| --- | --- |
| `DJANGO_SECRET_KEY` | Required when `DJANGO_DEBUG=false` |
| `DJANGO_DEBUG` | `true` locally, `false` in production |
| `DJANGO_ALLOWED_HOSTS` | Comma-separated host names |
| `DATABASE_URL` | Postgres URL (blank → local SQLite) |
| `REDIS_URL` | Cache backend (blank → in-memory) |
| `AI_API_KEY` / `AI_BASE_URL` / `AI_MODEL` | LLM provider |
| `SITE_URL` | Public base URL, used to build links in emails. Must be reachable from the device you open the link on, port included |
| `EMAIL_HOST` / `EMAIL_PORT` / `EMAIL_HOST_USER` / `EMAIL_HOST_PASSWORD` | SMTP for confirmation emails (blank → printed to the terminal) |
| `BREVO_API_KEY` | Brevo API key (used by the built-in Brevo email backend) |
| `EMAIL_VERIFICATION_REQUIRED` | `false` skips email confirmation (demos) |
| `PAYOUT_HOLD_DAYS` | Days a settled order waits before it can be paid out |
| `RAZORPAY_KEY_ID` / `RAZORPAY_KEY_SECRET` / `RAZORPAY_WEBHOOK_SECRET` | Gateway payments (optional) |
| `UPI_ID` / `UPI_PAYEE_NAME` | Direct UPI: the VPA buyers pay (optional) |

---

## Integrations

### Database — Neon (free managed Postgres)

1. Create a project at <https://neon.tech> and copy the connection string.
2. Put it in `.env` as `DATABASE_URL=postgresql://...?sslmode=require`.
3. Run `python manage.py migrate`.

Any Postgres works (Railway, Render, Supabase, self-hosted); only the URL
changes.

### Email — confirmation links

New accounts confirm their address once, by clicking a signed link, before they
can sign in (web or API). Locally the mail prints to your terminal, so no setup is
needed:

```bash
python manage.py runserver
# ...register at /register/ ...the link appears in the server console
```

In production set `SITE_URL` plus the `EMAIL_*` variables to a free SMTP relay
(Brevo, Resend or Mailtrap all have a free tier) so the links actually send.
Set `EMAIL_VERIFICATION_REQUIRED=false` to skip the step entirely.

---

### AI — SambaNova Cloud (free, OpenAI-compatible)

1. Sign up at <https://cloud.sambanova.ai> and create an API key.
2. Set `AI_API_KEY`, `AI_BASE_URL=https://api.sambanova.ai/v1` and
   `AI_MODEL=Meta-Llama-3.1-8B-Instruct`.
3. Leave `AI_API_KEY` blank to run on the built-in heuristics instead.

OpenAI, Groq, Together and any other OpenAI-compatible endpoint work by
changing `AI_BASE_URL`/`AI_MODEL`.

### Payments — pick one (or both)

**Direct UPI — free, no signup, no KYC**

1. Set `UPI_ID=<your VPA>`, e.g. `yourname@okaxis`, and `UPI_PAYEE_NAME`.
2. Buyers see a QR code / "Open UPI app" button on unpaid orders and submit the
   UTR from their app.
3. Staff upload the bank or UPI-app CSV at `/payments/reconcile/`; credits are
   matched to those UTRs, reviewed as a preview, then confirmed — which settles
   the orders. Wrong amounts, duplicates and unknown references are listed for
   manual review instead of being auto-settled.

**Razorpay — automatic UPI, cards and netbanking**

1. Create keys in the Razorpay dashboard.
2. Set `RAZORPAY_KEY_ID`, `RAZORPAY_KEY_SECRET` and `RAZORPAY_WEBHOOK_SECRET`.
3. Add a webhook pointing at `https://<your-domain>/payments/webhook/` for the
   `payment.captured` and `payment.failed` events.

Payments are verified twice: once in the browser callback (checkout signature)
and once server-to-server (webhook signature). Either way, the provider order id
must belong to the order being settled.

---

## REST API

Base path `/api/`. Interactive endpoints return paginated JSON.

| Method | Path | Notes |
| --- | --- | --- |
| POST | `/api/auth/register/` | Create an account, returns JWT tokens |
| POST | `/api/auth/token/` | Obtain access/refresh tokens |
| POST | `/api/auth/token/refresh/` | Refresh an access token |
| GET | `/api/auth/me/` | Current user + profile |
| GET/POST | `/api/listings/` | Browse / create (farmers only) |
| POST | `/api/listings/{id}/suggest_price/` | AI price suggestion |
| POST | `/api/listings/{id}/forecast/` | Demand forecast for the crop |
| GET/POST | `/api/orders/` | List your orders / place an order (buyers) |
| POST | `/api/orders/{id}/advance_status/` | Move an order through its lifecycle |
| POST | `/api/orders/{id}/initiate_payment/` | Create a Razorpay order |
| POST | `/api/orders/{id}/verify_payment/` | Verify a checkout signature |
| POST | `/api/orders/{id}/upi_paid/` | Record a direct-UPI claim (UTR) |
| POST | `/api/shipments/plan/` | Plan an optimised shipment (staff) |
| POST | `/api/forecasts/generate/` | Generate and store a forecast |

Authenticate with `Authorization: Bearer <access>`.

---

## Deployment

### Docker Compose (any VPS)

```bash
cp .env.example .env         # set DJANGO_DEBUG=false, DJANGO_SECRET_KEY, etc.
docker compose up --build
```

This starts Postgres, Redis and the web app on port 8000. The entrypoint runs
migrations, collects static files and seeds crops on first boot.

### Render (free, one repo push)

`render.yaml` provisions a free Docker web service **and** a free Postgres
database, so there is nothing separate to create.

1. Push this repo to GitHub.
2. In Render: **New + → Blueprint** and select the repo. Render reads
   `render.yaml`, creates the database, and injects `DATABASE_URL`.
3. The first deploy builds the image, runs migrations, collects static files
   and seeds the reference crops (the entrypoint does all of this).
4. Add the secrets marked `sync: false` in **Environment**: `AI_API_KEY`,
   the `RAZORPAY_*` keys, and `DJANGO_SUPERUSER_USERNAME`/`PASSWORD`.

No Redis is required — leave `REDIS_URL` blank and the app uses an in-process
cache. The hostname is trusted automatically, and the default SSL redirect is
safe because Render forwards `X-Forwarded-Proto`.

**Free-tier realities to know about:**

- Render's free Postgres **expires after ~30 days** (you then upgrade or move
  the data). For a longer-lived free database, a free Neon/Supabase Postgres is
  a drop-in — just set `DATABASE_URL`.
- The free web service **sleeps after ~15 minutes idle**; the next request is
  slow to wake.
- The container filesystem is **ephemeral**, so uploaded listing photos do not
  survive a redeploy. Set `USE_S3=true` (with the `AWS_*` variables) for
  persistent media, or keep photos out of production until then.

### Any PaaS (Procfile)

`Procfile` provides `release` (migrate + collectstatic) and `web`
(gunicorn) processes for Heroku-style hosts.

### Production checklist

- [ ] `DJANGO_DEBUG=false` and a strong `DJANGO_SECRET_KEY`
- [ ] `DJANGO_ALLOWED_HOSTS` and `DJANGO_CSRF_TRUSTED_ORIGINS` set to your domain
- [ ] `DATABASE_URL` points at managed Postgres
- [ ] `REDIS_URL` configured if you run more than one instance (optional otherwise)
- [ ] HTTPS terminated upstream; SSL redirect and secure cookies are on by default
- [ ] `DJANGO_SUPERUSER_*` set once, then removed
- [ ] Razorpay webhook URL registered and verified
- [ ] Health check wired to `/healthz/`

---

## License

Provided as-is for the AgriMarket project.

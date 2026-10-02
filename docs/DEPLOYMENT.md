# Deploying AgriMarket

Three supported paths, cheapest first:

1. **Render** (free, zero-cost, ~5 minutes) — recommended to start.
2. **Docker on any VPS** — full control, ~$5/mo.
3. **Any PaaS** using the bundled `Procfile` (Heroku, Railway, etc.).

All three run the **same hardened settings** — production mode, HTTPS enforced,
secure cookies, HSTS, WhiteNoise static serving, health checks.

---

## 0. Before you deploy (do this once)

Generate a strong secret key:

```bash
python -c "import secrets; print(secrets.token_urlsafe(64))"
```

Decide your values for:

| Variable | Notes |
| --- | --- |
| `DJANGO_DEBUG` | Must be `false` in production |
| `DJANGO_SECRET_KEY` | The value you just generated |
| `DJANGO_ALLOWED_HOSTS` | Your domain(s), comma-separated |
| `DJANGO_CSRF_TRUSTED_ORIGINS` | `https://your-domain` |
| `DATABASE_URL` | Postgres URL (see §4) |
| `AI_API_KEY` | Optional — see `docs/AI.md` |
| `RAZORPAY_*` | Optional — needed only for online payment |

> If `DJANGO_DEBUG=false` and `DJANGO_SECRET_KEY` is not set, the app **refuses
> to start**. That is intentional — it prevents insecure deployments.

---

## 1. Deploy to Render (free)

The repo already contains `render.yaml`, which provisions a **free Docker web
service and a free Postgres database together**.

### Steps

1. Push the project to GitHub.
2. In Render: **New + → Blueprint** → select the repository.
3. Render reads `render.yaml`, creates the database, and injects `DATABASE_URL`.
4. When prompted for the `sync: false` variables, add:
   - `AI_API_KEY` (optional)
   - `RAZORPAY_KEY_ID`, `RAZORPAY_KEY_SECRET`, `RAZORPAY_WEBHOOK_SECRET` (optional)
   - `DJANGO_SUPERUSER_USERNAME` / `DJANGO_SUPERUSER_PASSWORD` (to auto-create your admin)
5. Click **Apply**. The first build will:
   - build the Docker image,
   - run migrations,
   - collect static files,
   - create the superuser (if provided),
   - seed the reference crops,
   - start gunicorn.

Your app is live at `https://<service-name>.onrender.com`.

### Free-tier facts to plan around

| Limit | Impact | Mitigation |
| --- | --- | --- |
| **Free Postgres expires after ~30 days** | Database is deleted | Move to a free Neon/Supabase Postgres and update `DATABASE_URL` (§4) |
| **Web service sleeps after ~15 min idle** | First request after idle is slow (~30s) | Expected; upgrade for always-on |
| **Ephemeral filesystem** | Uploaded photos vanish on redeploy | Set `USE_S3=true` (§6) |
| **No free Redis** | — | None needed; the app uses an in-process cache by default |

---

## 2. Deploy with Docker on a VPS

Works on any Linux box with Docker.

```bash
git clone <your-repo> && cd agri-marketplace
cp .env.example .env
```

Edit `.env` for production:

```env
DJANGO_DEBUG=false
DJANGO_SECRET_KEY=<your generated key>
DJANGO_ALLOWED_HOSTS=your-domain.com,www.your-domain.com
DJANGO_CSRF_TRUSTED_ORIGINS=https://your-domain.com
POSTGRES_DB=agrimarket
POSTGRES_USER=agrimarket
POSTGRES_PASSWORD=<something strong>
AI_API_KEY=            # optional
RAZORPAY_KEY_ID=       # optional
RAZORPAY_KEY_SECRET=   # optional
RAZORPAY_WEBHOOK_SECRET=
```

> `docker-compose.yml` sets `DJANGO_DEBUG=false` and builds `DATABASE_URL` and
> `REDIS_URL` from the compose services automatically.

Start it:

```bash
docker compose up -d --build
docker compose logs -f web
```

The app listens on port **8000**. Put nginx/HTTPS in front:

- A ready `nginx.conf` is included — it proxies to the app, serves `/static/`
  from the collected files, and forwards the `X-Forwarded-Proto` header that
  Django needs to detect HTTPS.
- For TLS, use Caddy, certbot/Let's Encrypt, or your cloud load balancer.

---

## 3. Deploy to a PaaS (Heroku-style)

The `Procfile` defines both processes:

```
release: python manage.py migrate --noinput && python manage.py collectstatic --noinput
web: gunicorn config.wsgi:application --config gunicorn.conf.py
```

Set the environment variables from §0 in your host's config, add a Postgres
add-on, and deploy. `runtime.txt` pins Python 3.12.

---

## 4. Database

The app reads a single `DATABASE_URL`. Anything Postgres-compatible works.

```env
DATABASE_URL=postgresql://user:password@host:5432/dbname?sslmode=require
```

- If `DATABASE_URL` is **blank**, it falls back to a local SQLite file (dev only).
- TLS (`sslmode=require`) is added automatically for non-local hosts.

### Free providers (no card needed)

| Provider | Free tier | Notes |
| --- | --- | --- |
| **Neon** | Generous, no expiry | Serverless Postgres; copy the pooled connection string |
| **Supabase** | Generous, no expiry | Postgres + extras |
| **Render Postgres** | Free, but expires after ~30 days | Fine to start |

To migrate from the expiring Render DB to Neon later: create the Neon database,
set `DATABASE_URL` to the new string, then run `python manage.py migrate` and
restore data (`pg_dump`/`pg_restore`) as needed. No code changes.

---

## 5. Static files and health checks

- **Static files:** WhiteNoise serves them directly from the app (compressed in
  production) — no separate CDN or nginx `STATIC_ROOT` wiring required.
  `collectstatic` runs automatically in the Docker entrypoint.
- **Health check:** `GET /healthz/` returns `200 {"status":"ok",...}` and checks
  the database. Point your load balancer / platform health check at it.

---

## 6. Media (uploaded listing photos)

On ephemeral hosts (Render free, most containers), uploaded files disappear on
redeploy. For durable media, enable S3-compatible storage:

```env
USE_S3=true
AWS_ACCESS_KEY_ID=...
AWS_SECRET_ACCESS_KEY=...
AWS_STORAGE_BUCKET_NAME=...
AWS_S3_REGION_NAME=...
# For non-AWS S3 (Cloudflare R2, Backblaze B2, DigitalOcean Spaces):
AWS_S3_ENDPOINT_URL=https://<accountid>.r2.cloudflarestorage.com
AWS_S3_CUSTOM_DOMAIN=<public-bucket-domain>
```

No code changes — `settings.py` switches the storage backend when `USE_S3=true`.

---

## 7. Payments (Razorpay / UPI)

1. Create live keys in the Razorpay dashboard.
2. Set `RAZORPAY_KEY_ID`, `RAZORPAY_KEY_SECRET`, `RAZORPAY_WEBHOOK_SECRET`.
3. Add a webhook pointing at `https://<your-domain>/payments/webhook/` for the
   events `payment.captured` and `payment.failed`.

Payments are verified twice — in the browser callback (checkout signature) and
server-to-server (webhook signature) — so a forged callback cannot mark an order
paid. If keys are absent, the pay button is hidden and nothing else changes.

---

## 8. First-boot tasks

```bash
# create an admin (or set DJANGO_SUPERUSER_* so the entrypoint does it)
python manage.py createsuperuser

# seed the crop catalogue (safe to re-run; production does this automatically)
python manage.py seed_data

# optional: load demo accounts + listings (do NOT do this on real production)
python manage.py seed_data --demo
```

Nothing else is required; migrations and static collection happen automatically
in the container entrypoint.

---

## 9. Updating and rolling back

- **Render / PaaS:** push to the connected branch; a new deploy builds and
  replaces the old one. Use the platform's "rollback to previous deploy".
- **Docker:** `git pull && docker compose up -d --build`.
- Migrations run on every start, so schema changes apply automatically.

---

## 10. Production security checklist

- [ ] `DJANGO_DEBUG=false`
- [ ] Strong `DJANGO_SECRET_KEY`, never committed
- [ ] `DJANGO_ALLOWED_HOSTS` and `DJANGO_CSRF_TRUSTED_ORIGINS` set to your domain
- [ ] HTTPS terminated upstream (`X-Forwarded-Proto` forwarded)
- [ ] `DATABASE_URL` points at managed Postgres with TLS
- [ ] `DJANGO_SUPERUSER_*` used once, then removed from the environment
- [ ] Razorpay webhook registered and signature-verified
- [ ] Media on S3 if uploads matter
- [ ] `/healthz/` wired to the platform health check
- [ ] Log aggregation enabled (the app logs to stdout)

Verify locally before shipping:

```bash
DJANGO_DEBUG=false DJANGO_SECRET_KEY=<key> python manage.py check --deploy
```

---

## 11. Troubleshooting

| Symptom | Cause | Fix |
| --- | --- | --- |
| `DisallowedHost` | Domain missing from `DJANGO_ALLOWED_HOSTS` | Add the exact hostname |
| App 500s on boot about `SECRET_KEY` | Production started without a key | Set `DJANGO_SECRET_KEY` |
| Redirect loop / stuck on HTTPS | Proxy not forwarding `X-Forwarded-Proto` | Enable the header in your proxy (`nginx.conf` already does) |
| CSRF failures on POST | Domain missing from `DJANGO_CSRF_TRUSTED_ORIGINS` | Add `https://your-domain` |
| Static files 404 | `collectstatic` didn't run | It runs in the entrypoint; check the deploy logs |
| `SSL SYSCALL error` / DB refused | Managed DB requires TLS | Keep `?sslmode=require` in `DATABASE_URL` |
| Pay button missing | Razorpay keys unset | Set the `RAZORPAY_*` variables |
| Forecasts say `heuristic` | `AI_API_KEY` unset | See `docs/AI.md` |

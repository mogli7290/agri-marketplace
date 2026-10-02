# Operations

Running a free-tier deployment is mostly about finding out about problems
before a customer does. This is the part of the app that does that.

## The watchdog

```bash
python manage.py check_services
```

```
  OK    database     postgresql reachable
  OK    migrations   up to date
  WARN  cache        in-process cache — rate limits are not shared across workers
  OK    email        BrevoEmailBackend
  OK    site_url     https://agrimarket.onrender.com
  OK    disk         68.1% free

All checks passed
```

### What it checks, and why each one earns its place

| Check | Fails when | Why it matters |
| --- | --- | --- |
| `database` | Postgres is unreachable, **or** you are on SQLite | A sleeping site and a dead database look identical from outside |
| `migrations` | A model change has no applied migration | The quietest Django failure: the code wants a column, the table lacks it, and it surfaces on one unlucky request |
| `cache` | Values do not survive a round-trip | Sessions and rate limits both depend on it |
| `email` | No Brevo key, or the console backend left in production | Every password reset and order notification depends on it, and it is set purely by environment variables |
| `site_url` | Wrong scheme, unparseable, or loopback | Loopback links work on your machine and on nobody else's |
| `disk` | Under 15% free | A full container takes the site down with an unhelpful error |

`site_url` earns its place because of a real incident: the site ran on port
8001 while every emailed link said port 8000. It cost an afternoon and the
symptom was a user reporting "site can't be reached".

### It only emails on a change

`ServiceCheck.record()` marks a row `is_transition` only when that check's
severity differs from its previous value. A first-ever result is **not** a
transition, so a fresh deploy does not fire one alert per check.

This matters more than it sounds. A watchdog that mails every five minutes for
six hours is a watchdog you learn to ignore, and then it is worse than none.

Results are pruned to the newest 500 rows, so an every-five-minute cron adds
about 105,000 rows a year and keeps the last few days.

### Where the alerts go

Every active superuser with an email address. Delivery uses the same Brevo
backend as everything else, and a failure there is logged and swallowed rather
than crashing the run — a dead relay is exactly when you least want an
exception.

**The Brevo free tier only delivers to your own address** until you verify a
sending domain. Until then the watchdog works, but it is effectively mailing
you and nobody else. See `NOTIFICATIONS.md` §7.

## Running it on a schedule

`render.yaml` includes a free cron job:

```yaml
- type: cron
  name: health-watch
  schedule: "*/5 * * * *"
  command: python manage.py check_services --exit-code
```

`--exit-code` makes the run exit non-zero on failure so the platform records
it. It is opt-in because Django treats a command's return value as stdout —
returning `1` from `handle()` crashes with a confusing `TypeError`.

Elsewhere, any scheduler works:

```cron
*/5 * * * * cd /srv/app && /usr/bin/python manage.py check_services --exit-code >> /var/log/watchdog.log 2>&1
```

## The keep-alive, and why it is off by default

A free Render web service sleeps after ~15 minutes idle, and the next visitor
waits ~30 seconds while it wakes. You can prevent that:

```bash
python manage.py check_services --keepalive
```

It requests `SITE_URL` once, which resets the idle timer.

**Read this before turning it on.** Render's free tier includes a monthly
instance-hours allowance — 750 hours. A month contains 744 hours. Keeping one
service awake continuously consumes 744 of those 750, leaving six hours of
margin for anything else you run. It is not a free trick; it is the entire
allowance spent on staying on.

So the defaults are deliberate:

- **Watchdog: on.** It runs every five minutes, catches the failures that
  matter, and costs a cold start each time.
- **Keep-alive: off.** If you do not need the site to answer instantly, a
  30-second wake-up is a far better trade than spending your whole allowance.

### If you would rather not sleep at all

Sleep is a Render policy, not a law of nature. **Koyeb** and **Fly.io** run
free web services that do not idle out. Moving is mostly `DATABASE_URL` plus a
`Procfile` you already have. The trade is giving up Render's managed Postgres
and its Blueprint.

## The database will expire

Whichever provider you use, put the expiry in a calendar the day you set it up.
Render's free Postgres is deleted after about 30 days; the site comes back up
with an empty database and no error more visible than that.

Neon and Supabase both have free Postgres tiers that do not expire. Migrating is
copying `DATABASE_URL` — no code changes. See `DEPLOYMENT.md` §4.

## Reading the history

In the admin, **Service checks** lists every run. The model is read-only by
design; rows come from the watchdog, not from you.

```python
from marketplace.models import ServiceCheck

ServiceCheck.overall()          # "ok" / "warn" / "fail"
ServiceCheck.latest()           # {name: newest row}
```

## Tests

```bash
python manage.py test marketplace.tests.test_health
```

Covers each check's ok/warn/fail behaviour, that alerts fire only on a
transition, that a persistent failure does not re-alert, that recovery is
announced, that a dead mail backend does not crash the command, and that a
check which raises is contained rather than taking the whole run down.
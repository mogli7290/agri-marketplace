"""Health checks for a deployment nobody is watching.

A free-tier site fails quietly. It sleeps, its database expires, a deploy lands
with a bad environment variable — and in every case the first person to find out
is a buyer whose order did not arrive. These checks turn "quietly broken" into
an email.

Each check returns a plain dict rather than raising: the caller needs a full
picture in one pass, and one broken dependency must not hide the others.

Severity, and what to do about it:

* ``ok``   — working.
* ``warn`` — degraded but usable; worth knowing, not worth waking anyone.
* ``fail`` — the feature is broken. These are the ones that get mailed.

The database and migration checks are the valuable ones. "The Postgres expired"
and "a model was added but the migration never ran" are exactly the failures
that otherwise show up as a traceback in production and nowhere else.
"""

from __future__ import annotations

import logging
import shutil
from urllib.parse import urlparse

from django.conf import settings
from django.core.cache import cache
from django.db import connection
from django.db.migrations.executor import MigrationExecutor

logger = logging.getLogger(__name__)

#: Below this the container is at risk of failing writes outright.
DISK_WARN_PERCENT = 15

LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "0.0.0.0", "::1"}


def _result(name: str, severity: str = "ok", detail: str = "") -> dict:
    return {"name": name, "severity": severity, "detail": detail}


def check_database() -> dict:
    """Can we actually reach and query the database?

    A sleeping site and a dead database look identical from the outside; this
    is the check that tells them apart.
    """
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            cursor.fetchone()
    except Exception as exc:  # noqa: BLE001 - the whole point is to survive this
        return _result("database", "fail", f"{type(exc).__name__}: {exc}"[:255])

    vendor = connection.vendor
    if vendor == "sqlite":
        # Fine for a laptop, but photos and orders live in a file that a
        # redeploy deletes. Worth a nudge before it becomes a surprise.
        return _result(
            "database", "warn", "SQLite — data is lost on redeploy; set DATABASE_URL"
        )
    return _result("database", "ok", f"{vendor} reachable")


def check_migrations() -> dict:
    """Are there model changes whose migration has not been applied?

    A missing migration is the quietest failure in Django: the code expects a
    column, the table does not have it, and the error surfaces on one unlucky
    request rather than at boot.
    """
    try:
        executor = MigrationExecutor(connection)
        targets = executor.loader.graph.leaf_nodes()
        plan = executor.migration_plan(targets)
    except Exception as exc:  # noqa: BLE001
        return _result("migrations", "fail", f"{type(exc).__name__}: {exc}"[:255])

    if plan:
        names = ", ".join(migration.name for migration, _ in plan[:3])
        return _result("migrations", "fail", f"not applied: {names}")
    return _result("migrations", "ok", "up to date")


def check_cache() -> dict:
    """Round-trip through the cache.

    Rate limits and sessions both depend on it, and ``LocMemCache`` hides a
    multi-worker deployment from you until traffic arrives.
    """
    probe = "health-probe"
    try:
        cache.set(probe, "ok", 30)
        if cache.get(probe) != "ok":
            return _result("cache", "fail", "wrote a value but did not read it back")
        cache.delete(probe)
    except Exception as exc:  # noqa: BLE001
        return _result("cache", "fail", f"{type(exc).__name__}: {exc}"[:255])

    backend = settings.CACHES.get("default", {}).get("BACKEND", "")
    if "LocMem" in backend and getattr(settings, "DEBUG", False) is False:
        return _result(
            "cache", "warn", "in-process cache — rate limits are not shared across workers"
        )
    return _result("cache", "ok", backend.rsplit(".", 1)[-1])


def check_email() -> dict:
    """Is there a way to send mail at all?

    Every password reset and every order notification depends on this, and it
    is configured purely by environment variables — the easiest thing to leave
    unset in a deploy.
    """
    backend = settings.EMAIL_BACKEND or ""
    if "console" in backend:
        if getattr(settings, "DEBUG", False):
            return _result("email", "ok", "console backend (development)")
        return _result("email", "fail", "console backend in production — no mail is sent")

    if "Brevo" in backend and not (settings.BREVO_API_KEY or settings.EMAIL_HOST_PASSWORD):
        return _result("email", "fail", "Brevo backend but BREVO_API_KEY is empty")

    if "smtp" in backend.lower() and not settings.EMAIL_HOST:
        return _result("email", "fail", "SMTP backend but EMAIL_HOST is empty")

    if "locmem" in backend:
        return _result("email", "warn", "locmem backend — mail goes nowhere")

    return _result("email", "ok", backend.rsplit(".", 1)[-1])


def check_site_url() -> dict:
    """Is ``SITE_URL`` something a recipient's browser can actually open?

    This is the exact failure that cost an afternoon once: the site ran on one
    port while every emailed link pointed at another. On Render it is derived
    automatically, but a VPS or a local tunnel has no such help.
    """
    raw = (getattr(settings, "SITE_URL", "") or "").rstrip("/")
    if not raw:
        return _result(
            "site_url", "warn", "unset — email links fall back to the request host"
        )

    parsed = urlparse(raw)
    if parsed.scheme not in ("http", "https"):
        return _result("site_url", "fail", f"scheme is {parsed.scheme!r}, not http(s)")
    if not parsed.hostname:
        return _result("site_url", "fail", f"cannot parse a host out of {raw}")

    if parsed.hostname.lower() in LOOPBACK_HOSTS:
        return _result(
            "site_url", "warn", f"loopback ({raw}) — links only work on this machine"
        )
    return _result("site_url", "ok", raw)


def check_disk() -> dict:
    """Free space on the volume holding the app.

    Free containers have a small filesystem and a full one takes the whole site
    down with an unhelpful error.
    """
    try:
        usage = shutil.disk_usage(settings.MEDIA_ROOT or "/")
    except Exception as exc:  # noqa: BLE001
        return _result("disk", "warn", f"could not measure: {exc}"[:255])

    free_percent = round(usage.free / usage.total * 100, 1)
    if free_percent < DISK_WARN_PERCENT:
        return _result(
            "disk", "fail", f"{free_percent}% free — uploads will start failing"
        )
    if free_percent < 25:
        return _result("disk", "warn", f"{free_percent}% free")
    return _result("disk", "ok", f"{free_percent}% free")


#: Every check, in the order they should appear on a status page. Cheap and
#: specific before broad and scary. Named by attribute rather than captured by
#: value so ``run_checks`` resolves them at call time -- that keeps a check
#: substitutable in tests without reaching into the tuple's contents.
CHECK_NAMES = (
    "database",
    "migrations",
    "cache",
    "email",
    "site_url",
    "disk",
)


def run_checks() -> list[dict]:
    """Run every check and return the results.

    Never raises. A watchdog that crashes when the thing it watches is broken
    is worse than no watchdog, because it fails silently instead of loudly.
    """
    results = []
    for name in CHECK_NAMES:
        check = globals().get(f"check_{name}")
        try:
            results.append(check())
        except Exception as exc:  # noqa: BLE001
            logger.exception("Health check %s raised", name)
            results.append(_result(name, "fail", str(exc)[:255]))
    return results


def overall(results: list[dict]) -> str:
    """Roll individual severities into one word."""
    severities = {result.get("severity", "ok") for result in results}
    if "fail" in severities:
        return "fail"
    if "warn" in severities:
        return "warn"
    return "ok"


def summary_line(results: list[dict]) -> str:
    """A one-line digest, for a cron log or an email subject."""
    failing = [r["name"] for r in results if r.get("severity") == "fail"]
    warning = [r["name"] for r in results if r.get("severity") == "warn"]
    if failing:
        return "FAILING: " + ", ".join(failing)
    if warning:
        return "DEGRADED: " + ", ".join(warning)
    return "All checks passed"
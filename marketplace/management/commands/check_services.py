"""Run the health checks, record them, and complain only when something changes.

    python manage.py check_services
    python manage.py check_services --keepalive
    python manage.py check_services --no-email

Meant to be run from cron every few minutes:

    */5 * * * * cd /app && python manage.py check_services --keepalive

Two jobs in one command because they share a schedule and a container:

**The watchdog.** ``run_checks()`` inspects the database, pending migrations,
the cache, mail configuration, ``SITE_URL`` and disk space. Results go into
``ServiceCheck``. An email is sent only on a *transition* — a check that stays
broken should not mail you every five minutes for six hours.

**The keep-alive.** A free web service sleeps after ~15 minutes idle and the
next visitor waits ~30 seconds while it wakes. A periodic request to its own
``SITE_URL`` prevents that.

A word of warning about the keep-alive, because it is a real cost rather than a
free trick: Render's free tier includes a monthly instance-hours allowance, and
24x7 occupies essentially all of it. See ``docs/OPERATIONS.md`` for the numbers
and for the sleep-free alternatives.
"""

from __future__ import annotations

import logging
import urllib.error
import urllib.request

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand
from django.utils import timezone

from marketplace.models import ServiceCheck
from marketplace.services import health

logger = logging.getLogger(__name__)

#: Generous enough for a cold start on a free tier, short enough that a hung
#: request does not stall the cron run behind it.
KEEPALIVE_TIMEOUT = 60


class Command(BaseCommand):
    help = "Check that the deployment is healthy, and optionally keep it awake."

    def add_arguments(self, parser):
        parser.add_argument(
            "--keepalive",
            action="store_true",
            help="Also request SITE_URL so a free-tier service does not sleep.",
        )
        parser.add_argument(
            "--no-email",
            action="store_true",
            help="Record results without mailing anyone (quiet cron runs).",
        )
        parser.add_argument(
            "--exit-code",
            action="store_true",
            help=(
                "Exit non-zero when a check is failing, so cron and CI can act on "
                "it. Off by default: Django treats a returned string as stdout."
            ),
        )
        parser.add_argument(
            "--keepalive-only",
            action="store_true",
            help="Skip the health checks; just ping. Useful for frequent runs.",
        )

    def handle(self, *args, **options):
        exit_code = 0
        summary = ""

        if not options["keepalive_only"]:
            results = health.run_checks()
            rows = ServiceCheck.record(results)
            transitions = [row for row in rows if row.is_transition]

            for row in rows:
                style = self.style.SUCCESS if row.ok else self.style.WARNING
                self.stdout.write(
                    style(f"  {row.severity.upper():5} {row.name:12} {row.detail}")
                )

            self.stdout.write("")
            summary = health.summary_line(results)
            self.stdout.write(summary)

            if transitions and not options["no_email"]:
                self._notify(transitions, results)

            if any(row.severity == "fail" for row in rows):
                exit_code = 1

        if options["keepalive"] or options["keepalive_only"]:
            ok, detail = self._ping()
            if ok:
                self.stdout.write(self.style.SUCCESS(f"  keepalive: {detail}"))
            else:
                self.stdout.write(self.style.WARNING(f"  keepalive: {detail}"))
                if options.get("exit_code"):
                    exit_code = 1

        # A non-string return value is written to stdout by Django and blows up,
        # so signal failure with SystemExit when asked rather than with a return.
        if options.get("exit_code") and exit_code:
            raise SystemExit(exit_code)
        return summary

    # ------------------------------------------------------------------
    def _ping(self) -> tuple[bool, str]:
        """Request our own homepage to stop the platform idling the instance.

        Failures are reported, never raised: a sleeping site and a dead
        network look the same from here, and the health checks are the part
        that can actually diagnose it.
        """
        url = (getattr(settings, "SITE_URL", "") or "").rstrip("/")
        if not url:
            return False, "SITE_URL is not set, nothing to ping"

        try:
            request = urllib.request.Request(url, headers={"User-Agent": "keepalive/1.0"})
            with urllib.request.urlopen(request, timeout=KEEPALIVE_TIMEOUT) as response:
                code = response.status
        except urllib.error.HTTPError as exc:
            return False, f"{url} returned HTTP {exc.code}"
        except Exception as exc:  # noqa: BLE001
            return False, f"{type(exc).__name__}: {exc}"[:255]

        if code >= 400:
            return False, f"{url} returned HTTP {code}"
        return True, f"{url} -> {code}"

    def _notify(self, transitions: list, results: list) -> None:
        """Email the superusers, but only about the rows that changed.

        Goes through the same backend as everything else, and swallows failure:
        a dead mail relay is precisely when you least want an exception.
        """
        User = get_user_model()
        recipients = list(
            User.objects.filter(is_superuser=True, is_active=True)
            .exclude(email="")
            .values_list("email", flat=True)
        )
        if not recipients:
            self.stdout.write(
                self.style.WARNING("  no superuser email addresses; not sending an alert")
            )
            return

        failing = [row for row in transitions if row.severity == "fail"]
        recovered = [row for row in transitions if row.severity != "fail"]
        subject = (
            f"{'DOWN' if failing else 'RECOVERED'}: {settings.SITE_NAME} health check"
        )

        try:
            from marketplace.services import messaging

            delivered = messaging.send_email(
                to=recipients,
                subject=subject,
                template="service_alert",
                context={
                    "subject_line": subject,
                    "headline": "A health check changed state.",
                    "failing": failing,
                    "recovered": recovered,
                    "results": results,
                    "site_url": getattr(settings, "SITE_URL", ""),
                    "checked_at": timezone.localtime().strftime("%Y-%m-%d %H:%M:%S %Z"),
                },
            )
            self.stdout.write(
                self.style.SUCCESS(f"  alerted {len(recipients)} superuser(s)")
                if delivered
                else self.style.WARNING("  alert not accepted by the mail backend")
            )
        except Exception as exc:  # noqa: BLE001
            logger.error("Could not send the health alert: %s", exc)
            self.stdout.write(self.style.WARNING(f"  alert failed: {exc}"))
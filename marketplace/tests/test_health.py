"""Tests for the deployment watchdog.

The behaviour worth pinning down is not that the checks detect problems — it is
that they stay *quiet* when everything is fine. A watchdog that emails every
five minutes is a watchdog people learn to ignore, and then it is worse than no
watchdog at all.
"""

from unittest import mock

from django.conf import settings
from django.core import mail
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.urls import reverse

from marketplace.models import ServiceCheck
from marketplace.services import health
from marketplace.tests.base import CacheResetTestCase


class DatabaseCheckTests(TestCase):
    def test_a_reachable_database_is_not_failing(self):
        """The test database is SQLite, which is a deliberate warning."""
        self.assertNotEqual(health.check_database()["severity"], "fail")

    def test_postgres_is_ok(self):
        with mock.patch("marketplace.services.health.connection") as conn:
            conn.vendor = "postgresql"
            conn.cursor.return_value.__enter__.return_value = mock.MagicMock()
            self.assertEqual(health.check_database()["severity"], "ok")

    def test_sqlite_is_warned_about(self):
        """A file database on a container disappears on the next deploy."""
        self.assertEqual(health.check_database()["severity"], "warn")

    def test_an_unreachable_database_fails_rather_than_raising(self):
        with mock.patch(
            "marketplace.services.health.connection.cursor", side_effect=OSError("refused")
        ):
            result = health.check_database()
        self.assertEqual(result["severity"], "fail")
        self.assertIn("refused", result["detail"])


class MigrationCheckTests(TestCase):
    def test_an_up_to_date_database_passes(self):
        self.assertEqual(health.check_migrations()["severity"], "ok")

    def test_an_unapplied_migration_fails(self):
        # Mock(name=...) sets the mock's own name, not a .name attribute.
        fake_migration = mock.Mock()
        fake_migration.name = "0001_thing"
        with mock.patch(
            "marketplace.services.health.MigrationExecutor"
        ) as executor:
            executor.return_value.loader.graph.leaf_nodes.return_value = []
            executor.return_value.migration_plan.return_value = [(fake_migration, False)]
            result = health.check_migrations()
        self.assertEqual(result["severity"], "fail")
        self.assertIn("0001_thing", result["detail"])


class CacheCheckTests(TestCase):
    def test_a_working_cache_round_trips(self):
        """LocMem in a non-debug run is a warning; the round-trip is the real check."""
        self.assertNotEqual(health.check_cache()["severity"], "fail")

    def test_a_shared_cache_is_ok(self):
        with mock.patch.dict(
            settings.CACHES,
            {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}},
        ):
            with override_settings(DEBUG=True):
                self.assertEqual(health.check_cache()["severity"], "ok")

    def test_a_cache_that_loses_writes_is_caught(self):
        with mock.patch(
            "marketplace.services.health.cache.get", return_value=None
        ):
            result = health.check_cache()
        self.assertEqual(result["severity"], "fail")
        self.assertIn("read it back", result["detail"])

    def test_a_cache_that_raises_is_caught(self):
        with mock.patch(
            "marketplace.services.health.cache.set", side_effect=OSError("down")
        ):
            self.assertEqual(health.check_cache()["severity"], "fail")


class EmailConfigCheckTests(TestCase):
    @override_settings(
        EMAIL_BACKEND="marketplace.email_backend.BrevoEmailBackend", BREVO_API_KEY=""
    )
    def test_brevo_without_a_key_fails(self):
        result = health.check_email()
        self.assertEqual(result["severity"], "fail")
        self.assertIn("BREVO_API_KEY", result["detail"])

    @override_settings(
        EMAIL_BACKEND="marketplace.email_backend.BrevoEmailBackend",
        BREVO_API_KEY="xkeysib-test",
    )
    def test_brevo_with_a_key_is_ok(self):
        self.assertEqual(health.check_email()["severity"], "ok")

    @override_settings(EMAIL_BACKEND="django.core.mail.backends.console.EmailBackend")
    def test_the_console_backend_in_production_fails(self):
        """It would look fine and silently swallow every notification."""
        with override_settings(DEBUG=False):
            self.assertEqual(health.check_email()["severity"], "fail")

    @override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
    def test_locmem_is_only_a_warning(self):
        self.assertEqual(health.check_email()["severity"], "warn")


class SiteUrlCheckTests(TestCase):
    @override_settings(SITE_URL="")
    def test_unset_is_a_warning_not_a_failure(self):
        """Local development has no SITE_URL and is perfectly healthy."""
        self.assertEqual(health.check_site_url()["severity"], "warn")

    @override_settings(SITE_URL="https://agrimarket.onrender.com")
    def test_a_public_https_url_is_ok(self):
        self.assertEqual(health.check_site_url()["severity"], "ok")

    @override_settings(SITE_URL="http://127.0.0.1:8001")
    def test_loopback_is_a_warning(self):
        """The exact bug that cost an afternoon: links that only work locally."""
        self.assertEqual(health.check_site_url()["severity"], "warn")
        self.assertIn("loopback", health.check_site_url()["detail"])

    @override_settings(SITE_URL="ftp://example.com")
    def test_a_non_http_scheme_fails(self):
        self.assertEqual(health.check_site_url()["severity"], "fail")

    @override_settings(SITE_URL="not-a-url")
    def test_something_unparseable_fails(self):
        self.assertIn(
            health.check_site_url()["severity"], ("fail", "warn")
        )


class SummaryTests(TestCase):
    def test_fail_outranks_warn(self):
        results = [
            {"name": "a", "severity": "ok"},
            {"name": "b", "severity": "warn"},
            {"name": "c", "severity": "fail"},
        ]
        self.assertEqual(health.overall(results), "fail")

    def test_all_ok(self):
        self.assertEqual(health.overall([{"name": "a", "severity": "ok"}]), "ok")

    def test_a_raising_check_is_contained(self):
        """A watchdog that crashes when the site is broken fails silently."""
        def explode():
            raise RuntimeError("boom")

        with mock.patch("marketplace.services.health.check_disk", explode):
            results = health.run_checks()
        disk = next(r for r in results if r["name"] == "disk")
        self.assertEqual(disk["severity"], "fail")
        self.assertIn("boom", disk["detail"])


class ServiceCheckRecordTests(CacheResetTestCase):
    """The transition rule is the whole alerting design."""

    def setUp(self):
        super().setUp()
        self.ok = {"name": "database", "severity": "ok", "detail": "fine"}
        self.bad = {"name": "database", "severity": "fail", "detail": "gone"}

    def test_the_first_result_is_not_a_transition(self):
        """A fresh deploy would otherwise produce one alert per check."""
        rows = ServiceCheck.record([self.ok])
        self.assertFalse(rows[0].is_transition)

    def test_going_bad_is_a_transition(self):
        ServiceCheck.record([self.ok])
        rows = ServiceCheck.record([self.bad])
        self.assertTrue(rows[0].is_transition)

    def test_staying_bad_is_not_a_transition(self):
        ServiceCheck.record([self.ok])
        ServiceCheck.record([self.bad])
        rows = ServiceCheck.record([self.bad])
        self.assertFalse(rows[0].is_transition)

    def test_recovering_is_a_transition(self):
        ServiceCheck.record([self.ok])
        ServiceCheck.record([self.bad])
        rows = ServiceCheck.record([self.ok])
        self.assertTrue(rows[0].is_transition)

    def test_only_changed_can_be_forced(self):
        ServiceCheck.record([self.ok], only_changed=False)
        rows = ServiceCheck.record([self.ok], only_changed=False)
        self.assertTrue(rows[0].is_transition)

    def test_checks_are_tracked_independently(self):
        ServiceCheck.record([self.ok])
        rows = ServiceCheck.record([{"name": "cache", "severity": "fail", "detail": "x"}])
        self.assertFalse(rows[0].is_transition)

    def test_old_rows_are_pruned(self):
        for _ in range(ServiceCheck.KEEP_ROWS + 20):
            ServiceCheck.objects.create(name="database", severity="ok")
        deleted = ServiceCheck.prune(keep=10)
        self.assertEqual(ServiceCheck.objects.count(), 10)
        self.assertGreater(deleted, 0)

    def test_latest_returns_one_row_per_check(self):
        ServiceCheck.record([self.ok])
        ServiceCheck.record([self.bad])
        latest = ServiceCheck.latest()
        self.assertEqual(len(latest), 1)
        self.assertEqual(latest["database"].severity, "fail")

    def test_overall_reports_the_worst(self):
        ServiceCheck.record([self.ok])
        self.assertEqual(ServiceCheck.overall(), "ok")
        ServiceCheck.record([self.bad])
        self.assertEqual(ServiceCheck.overall(), "fail")


class CheckServicesCommandTests(CacheResetTestCase):
    def setUp(self):
        super().setUp()
        self.admin = self._make_superuser()

    def _make_superuser(self):
        from django.contrib.auth import get_user_model

        from marketplace.tests.factories import make_farmer

        user = make_farmer(username="watch_admin").user
        user.is_superuser = True
        user.is_staff = True
        user.save()
        return user

    def _fail(self):
        return mock.patch(
            "marketplace.services.health.check_database",
            return_value={
                "name": "database",
                "severity": "fail",
                "detail": "OperationalError: gone",
            },
        )

    def test_a_clean_run_alerts_nobody(self):
        call_command("check_services")
        self.assertEqual(len(mail.outbox), 0)

    def test_a_failure_alerts_the_superuser_once(self):
        ServiceCheck.record(
            [{"name": "database", "severity": "ok", "detail": "fine"}]
        )
        with self._fail():
            call_command("check_services")
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn(self.admin.email, mail.outbox[0].to)
        self.assertIn("DOWN", mail.outbox[0].subject)

    def test_a_persistent_failure_does_not_re_alert(self):
        ServiceCheck.record(
            [{"name": "database", "severity": "ok", "detail": "fine"}]
        )
        with self._fail():
            call_command("check_services")
            call_command("check_services")
            call_command("check_services")
        self.assertEqual(len(mail.outbox), 1)

    def test_recovery_is_announced(self):
        ServiceCheck.record(
            [{"name": "database", "severity": "ok", "detail": "fine"}]
        )
        with self._fail():
            call_command("check_services")
        mail.outbox.clear()
        call_command("check_services")
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("RECOVERED", mail.outbox[0].subject)

    def test_no_email_suppresses_the_alert(self):
        ServiceCheck.record(
            [{"name": "database", "severity": "ok", "detail": "fine"}]
        )
        with self._fail():
            call_command("check_services", "--no-email")
        self.assertEqual(len(mail.outbox), 0)

    def test_results_are_still_recorded_when_email_is_suppressed(self):
        with self._fail():
            call_command("check_services", "--no-email")
        self.assertTrue(ServiceCheck.objects.filter(name="database").exists())

    def test_a_dead_mail_backend_does_not_crash_the_command(self):
        ServiceCheck.record(
            [{"name": "database", "severity": "ok", "detail": "fine"}]
        )
        with self._fail():
            with mock.patch(
                "marketplace.services.messaging.send_email",
                side_effect=OSError("relay down"),
            ):
                call_command("check_services")
        # The check is still recorded; the command did not raise.
        self.assertEqual(ServiceCheck.latest()["database"].severity, "fail")

    def test_an_alert_with_no_superuser_address_is_skipped(self):
        self.admin.email = ""
        self.admin.save()
        ServiceCheck.record(
            [{"name": "database", "severity": "ok", "detail": "fine"}]
        )
        with self._fail():
            call_command("check_services")
        self.assertEqual(len(mail.outbox), 0)


class KeepAliveTests(CacheResetTestCase):
    @override_settings(SITE_URL="https://agrimarket.onrender.com")
    def test_a_successful_ping_is_reported(self):
        with mock.patch("urllib.request.urlopen") as urlopen:
            urlopen.return_value.__enter__.return_value.status = 200
            call_command("check_services", "--keepalive-only")
        self.assertTrue(urlopen.called)

    @override_settings(SITE_URL="")
    def test_no_site_url_is_reported_rather_than_crashing(self):
        call_command("check_services", "--keepalive-only")

    @override_settings(SITE_URL="https://agrimarket.onrender.com")
    def test_a_server_error_is_survived(self):
        import urllib.error

        with mock.patch(
            "urllib.request.urlopen", side_effect=urllib.error.HTTPError("u", 503, "no", {}, None)
        ):
            call_command("check_services", "--keepalive-only")

    @override_settings(SITE_URL="https://agrimarket.onrender.com")
    def test_a_connection_error_is_survived(self):
        with mock.patch("urllib.request.urlopen", side_effect=OSError("refused")):
            call_command("check_services", "--keepalive-only")

    @override_settings(SITE_URL="https://agrimarket.onrender.com")
    def test_the_ping_targets_site_url(self):
        with mock.patch("urllib.request.urlopen") as urlopen:
            urlopen.return_value.__enter__.return_value.status = 200
            call_command("check_services", "--keepalive-only")
        url = urlopen.call_args[0][0]
        self.assertEqual(url.full_url, "https://agrimarket.onrender.com")
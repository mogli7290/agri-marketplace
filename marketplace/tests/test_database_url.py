"""Tests for parsing DATABASE_URL into a Django DATABASES entry.

This is the one function standing between a provider's connection string
and every query the app makes, and it is what has to change shape when the
database moves. It had no coverage at all, which is a poor place to find out
it mangles a URL after the old database has already expired.
"""

import os
from unittest import mock

from django.test import SimpleTestCase

from config.settings import _database_from_url


class DatabaseUrlTests(SimpleTestCase):
    def parse(self, url, **env):
        with mock.patch.dict(os.environ, env, clear=False):
            with mock.patch.dict(os.environ, {"DB_REQUIRE_SSL": ""}, clear=False):
                if "DB_REQUIRE_SSL" not in env:
                    # Default is "on"; drop the caller's empty marker first.
                    os.environ.pop("DB_REQUIRE_SSL", None)
                return _database_from_url(url)

    # -- the shapes real providers hand out ---------------------------------

    def test_a_neon_url_without_a_port(self):
        """Neon omits the port. Django drops an empty one and libpq uses 5432."""
        config = self.parse(
            "postgresql://u:p@ep-cool-name-123456.us-east-2.aws.neon.tech/neondb?sslmode=require"
        )
        self.assertEqual(config["ENGINE"], "django.db.backends.postgresql")
        self.assertEqual(config["NAME"], "neondb")
        self.assertEqual(config["HOST"], "ep-cool-name-123456.us-east-2.aws.neon.tech")
        self.assertEqual(config["PORT"], "")
        self.assertEqual(config["OPTIONS"]["sslmode"], "require")

    def test_a_supabase_url_with_a_port(self):
        config = self.parse("postgresql://postgres.pw:x@db.abcdefg.supabase.co:5432/postgres")
        self.assertEqual(config["NAME"], "postgres")
        self.assertEqual(config["PORT"], "5432")
        self.assertEqual(config["OPTIONS"]["sslmode"], "require")

    def test_a_render_url(self):
        config = self.parse("postgresql://agrimarket:s@dpg-abc.oregon.render.com:5432/agrimarket")
        self.assertEqual(config["HOST"], "dpg-abc.oregon.render.com")
        self.assertEqual(config["USER"], "agrimarket")
        self.assertEqual(config["PASSWORD"], "s")

    def test_the_postgres_scheme_alias_works(self):
        self.assertEqual(
            self.parse("postgres://u:p@h:5432/db")["ENGINE"],
            "django.db.backends.postgresql",
        )

    def test_postgis_selects_the_gis_backend(self):
        self.assertEqual(
            self.parse("postgis://u:p@h:5432/db")["ENGINE"],
            "django.contrib.gis.db.backends.postgis",
        )

    def test_an_unknown_scheme_falls_back_to_postgresql(self):
        """Better a working connection than a hard failure on a typo'd scheme."""
        self.assertEqual(
            self.parse("weird://u:p@h:5432/db")["ENGINE"],
            "django.db.backends.postgresql",
        )

    # -- TLS -----------------------------------------------------------------

    def test_localhost_is_exempt_from_tls(self):
        """A local socket or tunnel has no certificate to verify."""
        self.assertNotIn("sslmode", self.parse("postgresql://u:p@localhost:5432/db")["OPTIONS"])
        self.assertNotIn("sslmode", self.parse("postgresql://u:p@127.0.0.1:5432/db")["OPTIONS"])

    def test_ssl_can_be_turned_off_for_a_provider_that_needs_it_off(self):
        config = self.parse("postgresql://u:p@db.internal:5432/db", DB_REQUIRE_SSL="false")
        self.assertEqual(config["OPTIONS"], {})

    # -- connection reuse ----------------------------------------------------

    def test_connection_max_age_is_configurable(self):
        """Neon's pooled endpoint is a transaction-mode proxy.

        Holding a connection open across a transaction-pooling proxy is how
        you get intermittent "prepared statement does not exist" errors, so
        this has to be droppable to 0 without a code change.
        """
        self.assertEqual(
            self.parse("postgresql://u:p@h/db", DB_CONN_MAX_AGE="0")["CONN_MAX_AGE"], 0
        )
        self.assertEqual(self.parse("postgresql://u:p@h/db")["CONN_MAX_AGE"], 600)

    def test_an_ipv6_host_is_parsed(self):
        config = self.parse("postgresql://u:p@[2001:db8::1]:5432/db")
        self.assertEqual(config["HOST"], "2001:db8::1")
        self.assertEqual(config["PORT"], "5432")
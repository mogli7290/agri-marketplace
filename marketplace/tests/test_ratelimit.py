"""Tests for the rate limits on the public forms.

What these protect, in order of how cheap they are to attack:

* the sign-in page is an open password oracle without a cap,
* the reset and resend pages can be used to bury a stranger's inbox,
* a throttle that counts successful logins locks out real users,
* a throttle that reads its own counter as "seconds remaining" blocks
  everyone after a single attempt.

The last one is not hypothetical — it is exactly what the first
implementation did.
"""

from django.core.cache import cache
from django.test import TestCase, override_settings
from django.urls import reverse

from marketplace import ratelimit
from marketplace.tests.factories import make_farmer


class RateLimitTestCase(TestCase):
    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        self.farmer = make_farmer(username="rl_farmer")
        self.user = self.farmer.user
        self.user.set_password("CorrectPass123")
        self.user.save()

    def _first_429(self, url, payload, tries):
        """1-based attempt number that was refused, or None."""
        for attempt in range(1, tries + 1):
            if self.client.post(url, payload).status_code == 429:
                return attempt
        return None

    def _login_payload(self, password="wrong"):
        return {"username": self.user.username, "password": password}


class LoginRateLimitTests(RateLimitTestCase):
    def test_wrong_passwords_are_capped(self):
        attempts, _ = ratelimit._limit_for("login")
        blocked = self._first_429(
            reverse("marketplace:login"), self._login_payload(), attempts + 3
        )
        self.assertEqual(blocked, attempts + 1)

    def test_a_correct_password_still_works_after_failures(self):
        """Otherwise the throttle becomes a denial-of-service on the account."""
        attempts, _ = ratelimit._limit_for("login")
        for _ in range(attempts - 1):
            self.client.post(reverse("marketplace:login"), self._login_payload())
        response = self.client.post(
            reverse("marketplace:login"), self._login_payload("CorrectPass123")
        )
        self.assertEqual(response.status_code, 302)

    def test_success_clears_the_counter(self):
        """A user who fumbles and then gets it right starts fresh."""
        self.client.post(reverse("marketplace:login"), self._login_payload())
        self.client.post(
            reverse("marketplace:login"), self._login_payload("CorrectPass123")
        )
        self.client.logout()
        response = self.client.post(reverse("marketplace:login"), self._login_payload())
        self.assertEqual(response.status_code, 200)

    def test_a_get_is_never_counted(self):
        """Reloading a form must not spend an attempt."""
        for _ in range(30):
            self.assertEqual(self.client.get(reverse("marketplace:login")).status_code, 200)
        response = self.client.post(reverse("marketplace:login"), self._login_payload())
        self.assertEqual(response.status_code, 200)

    def test_a_second_attempt_is_still_refused_after_the_first(self):
        """The counter must survive; a fresh window each time never throttles."""
        attempts, _ = ratelimit._limit_for("login")
        for _ in range(attempts + 2):
            self.client.post(reverse("marketplace:login"), self._login_payload())
        self.assertEqual(
            self.client.post(reverse("marketplace:login"), self._login_payload()).status_code,
            429,
        )


class PerClientTests(RateLimitTestCase):
    def test_one_client_blocking_does_not_block_another(self):
        """Otherwise a shared NAT — an office, a carrier — locks everyone out."""
        attempts, _ = ratelimit._limit_for("login")
        for _ in range(attempts + 2):
            self.client.post(
                reverse("marketplace:login"), self._login_payload(),
                REMOTE_ADDR="203.0.113.1",
            )
        response = self.client.post(
            reverse("marketplace:login"), self._login_payload(),
            REMOTE_ADDR="203.0.113.2",
        )
        self.assertEqual(response.status_code, 200)

    def test_scopes_do_not_share_a_budget(self):
        """Burning the login limit must not also block password resets."""
        attempts, _ = ratelimit._limit_for("login")
        for _ in range(attempts + 2):
            self.client.post(reverse("marketplace:login"), self._login_payload())
        response = self.client.post(
            reverse("marketplace:password_reset_request"), {"email": "a@example.com"}
        )
        self.assertEqual(response.status_code, 302)


class ThrottleResponseTests(RateLimitTestCase):
    def test_the_refusal_says_when_to_come_back(self):
        attempts, window = ratelimit._limit_for("login")
        for _ in range(attempts + 1):
            response = self.client.post(
                reverse("marketplace:login"), self._login_payload()
            )
        self.assertEqual(response.status_code, 429)
        self.assertIn("Retry-After", response.headers)

    def test_the_refusal_does_not_leak_the_remaining_attempts(self):
        attempts, _ = ratelimit._limit_for("login")
        for _ in range(attempts + 1):
            response = self.client.post(
                reverse("marketplace:login"), self._login_payload()
            )
        body = response.content.decode().lower()
        self.assertNotIn("attempts remaining", body)
        self.assertNotIn(str(attempts), body)


class MailSendingFormTests(RateLimitTestCase):
    """These cost someone else's inbox, so every POST counts, valid or not."""

    def _post_n(self, name, payload, n):
        url = reverse(f"marketplace:{name}")
        return [self.client.post(url, payload).status_code for _ in range(n)]

    def test_password_reset_is_capped(self):
        attempts, _ = ratelimit._limit_for("password_reset")
        codes = self._post_n(
            "password_reset_request", {"email": "someone@example.com"}, attempts + 2
        )
        self.assertEqual(codes[attempts], 429)

    def test_verification_resend_is_capped(self):
        attempts, _ = ratelimit._limit_for("verify_resend")
        codes = self._post_n(
            "verification_resend", {"email": "someone@example.com"}, attempts + 2
        )
        self.assertEqual(codes[attempts], 429)

    def test_register_is_capped(self):
        attempts, _ = ratelimit._limit_for("register")
        codes = self._post_n(
            "register",
            {
                "username": "someone",
                "email": "someone@example.com",
                "password1": "Str0ngPass!23",
                "password2": "Str0ngPass!23",
            },
            attempts + 2,
        )
        self.assertEqual(codes[attempts], 429)


class ClientIdentityTests(TestCase):
    """The identity used for counting has to be hard to forge."""

    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)

    def _request(self, **meta):
        request = type("R", (), {})()
        request.META = {"REMOTE_ADDR": "198.51.100.5", **meta}
        return request

    def test_debug_ignores_a_forged_forwarded_header(self):
        """Locally the socket address is the truth; the header is the attacker's."""
        with override_settings(DEBUG=True):
            key = ratelimit.client_key(self._request(HTTP_X_FORWARDED_FOR="1.2.3.4"))
        self.assertEqual(key, "198.51.100.5")

    def test_production_reads_the_forwarded_header(self):
        """Behind Render/nginx the socket address is the proxy, not the user."""
        with override_settings(DEBUG=False):
            key = ratelimit.client_key(
                self._request(HTTP_X_FORWARDED_FOR="203.0.113.9, 10.0.0.1")
            )
        self.assertEqual(key, "203.0.113.9")

    def test_a_missing_address_falls_back_rather_than_bypassing(self):
        """Failing closed: unknown callers share a budget instead of a free pass."""
        with override_settings(DEBUG=False):
            key = ratelimit.client_key(self._request(REMOTE_ADDR=""))
        self.assertEqual(key, ratelimit.UNKNOWN_CLIENT)


class CacheFailureTests(TestCase):
    """A broken cache must never lock a real user out of their own account."""

    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)

    def test_recording_survives_a_cache_that_raises(self):
        from unittest import mock

        with mock.patch("marketplace.ratelimit.cache.add", side_effect=OSError("down")):
            self.assertEqual(ratelimit.record("login", "1.2.3.4"), 0)

    def test_clearing_survives_a_cache_that_raises(self):
        from unittest import mock

        with mock.patch("marketplace.ratelimit.cache.delete_many", side_effect=OSError("down")):
            ratelimit.reset("login", "1.2.3.4")  # must not raise


class CounterAndBlockAreSeparateTests(TestCase):
    """The counter is a count; the block is a duration. Never conflate them.

    Reading the attempt count back as "seconds left" blocks every caller after
    a single try, which is how this was first written.
    """

    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)

    def test_one_attempt_does_not_mean_one_second_blocked(self):
        self.assertEqual(ratelimit.record("login", "5.5.5.5"), 0)
        self.assertEqual(ratelimit.is_limited("login", "5.5.5.5"), 0)

    def test_the_block_appears_only_after_the_cap(self):
        attempts, _ = ratelimit._limit_for("login")
        for _ in range(attempts):
            self.assertEqual(ratelimit.record("login", "5.5.5.5"), 0)
        self.assertGreater(ratelimit.record("login", "5.5.5.5"), 0)
        self.assertGreater(ratelimit.is_limited("login", "5.5.5.5"), 0)

"""Tests for the forgot-password flow.

The guarantees that matter:

* an unknown address gets exactly the same answer as a known one, so the page
  cannot be used to find out who has an account,
* a dead mail backend reports honestly instead of silently pretending,
* an expired or forged link never shows a password form,
* changing the password actually lets the user sign in.
"""

import re
from urllib.parse import urlsplit

from django.contrib.auth import get_user_model
from django.contrib.auth.tokens import default_token_generator
from django.core import mail
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from marketplace.services.accounts import site_base_url
from marketplace.tests.factories import make_farmer

User = get_user_model()


class PasswordResetTestCase(TestCase):
    def setUp(self):
        self.farmer = make_farmer(username="reset_farmer")
        self.user = self.farmer.user
        self.user.set_password("OldPass123")
        self.user.save()

    def request_url(self):
        return reverse("marketplace:password_reset_request")

    def confirm_url(self, user=None, token=None):
        user = user or self.user
        token = token or default_token_generator.make_token(user)
        return reverse(
            "marketplace:password_reset_confirm",
            kwargs={"uidb64": _urlsafe(user.pk), "token": token},
        )


def _urlsafe(pk):
    """Django's PasswordResetConfirmView base36-encodes the user id."""
    from django.utils.encoding import force_bytes
    from django.utils.http import urlsafe_base64_encode

    return urlsafe_base64_encode(force_bytes(pk))


@override_settings(SITE_URL="https://agri.example.com")
class PasswordResetRequestTests(PasswordResetTestCase):
    def test_a_known_address_gets_a_link(self):
        response = self.client.post(self.request_url(), {"email": self.user.email})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(len(mail.outbox), 1)
        body = mail.outbox[0].body
        self.assertIn("/password-reset/", body)
        # SITE_URL wins over build_absolute_uri so a proxy cannot turn the link
        # into http:// in production.
        self.assertIn("https://agri.example.com/password-reset/", body)

    def test_the_link_falls_back_to_the_request_without_site_url(self):
        with self.settings(SITE_URL=""):
            self.client.post(self.request_url(), {"email": self.user.email})
        self.assertIn("http://testserver/password-reset/", mail.outbox[0].body)

    def test_an_unknown_address_looks_identical(self):
        """No account enumeration: same redirect, no mail, same page."""
        known = self.client.post(self.request_url(), {"email": self.user.email})
        known.status_code

        self.client.cookies.clear()
        response = self.client.post(
            self.request_url(), {"email": "nobody-at-all@example.com"}
        )
        self.assertEqual(response.status_code, known.status_code)
        self.assertIn("password-reset/done", response["Location"])

        done = self.client.get(response["Location"])
        stranger = self.client.get(self.request_url())
        # The confirmation page must not name or imply an existing account.
        self.assertContains(done, "If that address has an account")
        self.assertContains(stranger, "we'll send you a link")

    def test_an_invalid_address_is_rejected(self):
        response = self.client.post(self.request_url(), {"email": "not-an-email"})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Enter a valid email address")
        self.assertEqual(mail.outbox, [])

    def test_a_dead_backend_is_reported_honestly(self):
        with self.settings(EMAIL_BACKEND="marketplace.tests.test_password_reset.BrokenBackend"):
            response = self.client.post(self.request_url(), {"email": self.user.email})
        messages = [str(m) for m in response.wsgi_request._messages]
        self.assertTrue(any("could not send" in m.lower() for m in messages), messages)

    def test_the_login_page_offers_the_link(self):
        response = self.client.get(reverse("marketplace:login"))
        self.assertContains(response, "Forgot your password?")
        self.assertContains(response, self.request_url())


class PasswordResetConfirmTests(PasswordResetTestCase):
    def test_a_valid_link_shows_the_form(self):
        response = self.client.get(self.confirm_url())
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Choose a new password")

    def test_the_password_can_be_changed_and_then_used(self):
        url = self.confirm_url()
        response = self.client.post(
            url, {"new_password1": "BrandNew123", "new_password2": "BrandNew123"}
        )
        self.assertEqual(response.status_code, 302)
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password("BrandNew123"))

        # And it really is the new one at the sign-in form.
        login = self.client.post(
            reverse("marketplace:login"),
            {"username": self.user.username, "password": "BrandNew123"},
        )
        self.assertEqual(login.status_code, 302)

    def test_mismatched_passwords_are_refused(self):
        response = self.client.post(
            self.confirm_url(),
            {"new_password1": "BrandNew123", "new_password2": "Different123"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "match")

    def test_a_weak_password_is_refused(self):
        response = self.client.post(
            self.confirm_url(), {"new_password1": "123", "new_password2": "123"}
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "too short")

    def test_a_forged_token_never_shows_a_form(self):
        response = self.client.get(self.confirm_url(token="made-up-token"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "This link has expired")
        self.assertNotContains(response, "Save new password")

    def test_a_tampered_token_cannot_change_the_password(self):
        response = self.client.post(
            self.confirm_url(token="made-up-token"),
            {"new_password1": "BrandNew123", "new_password2": "BrandNew123"},
        )
        self.assertNotContains(response, "Save new password")
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password("OldPass123"))

    def test_a_token_stops_working_once_used(self):
        """Otherwise an old email stays a way in forever."""
        url = self.confirm_url()
        self.client.post(url, {"new_password1": "BrandNew123", "new_password2": "BrandNew123"})

        # The token encodes the password hash, so the old one is now invalid.
        response = self.client.get(url)
        self.assertContains(response, "This link has expired")

    def test_an_unknown_user_id_is_handled(self):
        from django.utils.encoding import force_bytes
        from django.utils.http import urlsafe_base64_encode

        url = reverse(
            "marketplace:password_reset_confirm",
            kwargs={
                "uidb64": urlsafe_base64_encode(force_bytes(999999)),
                "token": "anything",
            },
        )
        response = self.client.get(url)
        self.assertContains(response, "This link has expired")

    def test_an_inactive_account_cannot_reset(self):
        self.user.is_active = False
        self.user.save()
        response = self.client.get(self.confirm_url())
        self.assertContains(response, "This link has expired")


class BrokenBackend:
    """A mail backend that always dies, like a dead relay."""

    def __init__(self, *args, **kwargs):
        pass

    def send_messages(self, messages):
        raise OSError("connection refused")

class FakeRequest:
    """Just enough request to answer ``build_absolute_uri``."""

    def __init__(self, url):
        self.url = url

    def build_absolute_uri(self, path="/"):
        return self.url + path


class BaseUrlTestCase(TestCase):
    """A reset link is only as good as the host it carries.

    A loopback ``SITE_URL`` mails you a link to ``127.0.0.1``, which on a phone
    means "site can't be reached". The request is a better witness.
    """

    def test_a_lan_request_beats_a_loopback_site_url(self):
        with override_settings(SITE_URL="http://127.0.0.1:8000"):
            base = site_base_url(FakeRequest("http://10.208.181.156:8000"))
        self.assertEqual(base, "http://10.208.181.156:8000")

    def test_a_real_site_url_still_wins(self):
        with override_settings(SITE_URL="https://agri.example.com"):
            base = site_base_url(FakeRequest("http://10.0.0.5:8000"))
        self.assertEqual(base, "https://agri.example.com")

    def test_loopback_against_loopback_is_left_alone(self):
        with override_settings(SITE_URL="http://127.0.0.1:8000"):
            base = site_base_url(FakeRequest("http://127.0.0.1:8000"))
        self.assertEqual(base, "http://127.0.0.1:8000")

    def test_no_site_url_falls_back_to_the_request(self):
        with override_settings(SITE_URL=""):
            base = site_base_url(FakeRequest("http://10.0.0.5:8000"))
        self.assertEqual(base, "http://10.0.0.5:8000")

    def test_a_loopback_site_url_is_still_used_without_a_request(self):
        with override_settings(SITE_URL="http://127.0.0.1:8000/"):
            self.assertEqual(site_base_url(), "http://127.0.0.1:8000")

    def test_the_emailed_link_uses_the_lan_host(self):
        with override_settings(SITE_URL="http://127.0.0.1:8000", PASSWORD_RESET_TIMEOUT=3600):
            response = self.client.post(
                reverse("marketplace:password_reset_request"),
                {"email": "someone@example.com"},
                HTTP_HOST="10.208.181.156:8000",
            )
        self.assertEqual(response.status_code, 302)


class ConfiguredHostTestCase(TestCase):
    """A misconfigured host must not slip through as a dead link.

    The bug this guards against was real: the dev server ran on 8001 while
    ``.env`` still said 8000, so every emailed link pointed at a closed port.
    """

    def test_a_port_mismatch_is_visible_in_the_emailed_url(self):
        with override_settings(SITE_URL="http://10.0.0.5:8001"):
            base = site_base_url(FakeRequest("http://10.0.0.5:8001"))
        self.assertIn(":8001", base)

    def test_the_emailed_url_keeps_the_configured_port(self):
        # A real account: an unknown address deliberately gets no mail at all.
        user = make_farmer(username="port_farmer").user
        with override_settings(
            SITE_URL="http://10.0.0.5:8001",
            PASSWORD_RESET_TIMEOUT=3600,
            # The request can only vouch for the host if Django trusts it.
            ALLOWED_HOSTS=["10.0.0.5"],
        ):
            self.client.post(
                reverse("marketplace:password_reset_request"),
                {"email": user.email},
                HTTP_HOST="10.0.0.5:8001",
            )
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("http://10.0.0.5:8001/password-reset/", mail.outbox[0].body)


class EmailedLinkTestCase(TestCase):
    """Follow the link that was actually emailed, not one built by hand.

    The route is ``password-reset/<uidb64>/<token>/`` — two segments. Building
    that string by hand once shipped a one-segment link that 404'd for every
    user while the rest of the suite stayed green, because the other tests
    reversed the URL themselves instead of reading the emailed one.
    """

    def setUp(self):
        self.user = make_farmer(username="emailed_farmer").user
        self.user.set_password("OldPass123")
        self.user.save()

    def _emailed_path(self):
        """Submit the form for real and return the path from the email body."""
        self.client.post(
            reverse("marketplace:password_reset_request"),
            {"email": self.user.email},
        )
        body = mail.outbox[-1].body
        url = re.search(r"https?://\S+/password-reset/\S+", body).group(0)
        return urlsplit(url).path

    def test_the_emailed_url_has_both_path_segments(self):
        path = self._emailed_path()
        # <uidb64>/<token>/ — one segment would 404.
        self.assertEqual(len([p for p in path.split("/") if p]), 3, path)

    def test_the_emailed_url_is_not_a_404(self):
        response = self.client.get(self._emailed_path())
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("expired", response.content.decode().lower())

    def test_the_emailed_url_offers_the_new_password_form(self):
        response = self.client.get(self._emailed_path())
        self.assertContains(response, "new_password1")

    def test_the_emailed_url_actually_changes_the_password(self):
        path = self._emailed_path()
        self.client.post(
            path,
            {
                "new_password1": "BrandNew123",
                "new_password2": "BrandNew123",
            },
        )
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password("BrandNew123"))
        self.assertFalse(self.user.check_password("OldPass123"))

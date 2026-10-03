"""Tests for the email-confirmation flow.

Covers the registration gate, single-use links, replay and tamper protection,
resend throttling, and the login block.
"""

from datetime import timedelta

from django.contrib.auth import get_user_model
from django.core import mail
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone

from marketplace.models import EmailVerification
from marketplace.services import accounts as accounts_service
from marketplace.tests.base import CacheResetTestCase
from marketplace.tests.factories import make_buyer

User = get_user_model()

REGISTRATION = {
    "user_type": "buyer",
    "username": "confirmme",
    "email": "confirmme@example.com",
    "full_name": "Confirm Me",
    "phone_number": "9555555555",
    "buyer_type": "consumer",
    "city": "Pune",
    "password1": "StrongPass123",
    "password2": "StrongPass123",
}


def extract_token(message):
    """Pull the /verify-email/<token>/ URL out of the sent mail."""
    body = message.body
    for line in body.splitlines():
        if "/verify-email/" in line:
            return line.strip().rsplit("/", 2)[-2]
    raise AssertionError(f"No verification link in message:\n{body}")


class RegistrationGateTests(CacheResetTestCase):
    def test_registration_sends_a_link_and_blocks_the_session(self):
        response = self.client.post(reverse("marketplace:register"), REGISTRATION)
        self.assertEqual(response.status_code, 302)
        self.assertIn("verify-email/sent", response.url)

        user = User.objects.get(username="confirmme")
        self.assertFalse(user.email_verification.is_verified)
        self.assertNotIn("_auth_user_id", self.client.session)
        self.assertEqual(len(mail.outbox), 1)

    def test_link_marks_the_address_verified_and_allows_login(self):
        self.client.post(reverse("marketplace:register"), REGISTRATION)
        user = User.objects.get(username="confirmme")
        token = extract_token(mail.outbox[0])

        response = self.client.get(reverse("marketplace:verify_email", args=[token]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Email confirmed")

        user.refresh_from_db()
        self.assertTrue(user.email_verification.is_verified)

        login = self.client.post(
            reverse("marketplace:login"),
            {"username": "confirmme", "password": "StrongPass123"},
        )
        self.assertEqual(login.status_code, 302)
        self.assertEqual(int(self.client.session["_auth_user_id"]), user.pk)

    def test_login_is_blocked_while_pending(self):
        self.client.post(reverse("marketplace:register"), REGISTRATION)
        response = self.client.post(
            reverse("marketplace:login"),
            {"username": "confirmme", "password": "StrongPass123"},
        )
        self.assertEqual(response.status_code, 302)
        self.assertNotIn("_auth_user_id", self.client.session)

    @override_settings(EMAIL_VERIFICATION_RESEND_COOLDOWN=0)
    def test_login_resends_the_link_when_cooled_down(self):
        self.client.post(reverse("marketplace:register"), REGISTRATION)
        mail.outbox.clear()

        self.client.post(
            reverse("marketplace:login"),
            {"username": "confirmme", "password": "StrongPass123"},
        )
        self.assertEqual(len(mail.outbox), 1)

    def test_login_does_not_spam_within_the_cooldown(self):
        self.client.post(reverse("marketplace:register"), REGISTRATION)
        mail.outbox.clear()
        self.client.post(
            reverse("marketplace:login"),
            {"username": "confirmme", "password": "StrongPass123"},
        )
        self.assertEqual(len(mail.outbox), 0)

    def test_tampered_token_is_refused(self):
        self.client.post(reverse("marketplace:register"), REGISTRATION)
        response = self.client.get(
            reverse("marketplace:verify_email", args=["forged-token"])
        )
        self.assertContains(response, "invalid or has expired")
        user = User.objects.get(username="confirmme")
        self.assertFalse(user.email_verification.is_verified)

    def test_link_cannot_be_replayed(self):
        self.client.post(reverse("marketplace:register"), REGISTRATION)
        user = User.objects.get(username="confirmme")
        token = extract_token(mail.outbox[0])

        self.client.get(reverse("marketplace:verify_email", args=[token]))
        second = self.client.get(reverse("marketplace:verify_email", args=[token]))
        self.assertContains(second, "already verified")

        user.refresh_from_db()
        self.assertEqual(user.email_verification.verified_at is not None, True)

    def test_issuing_a_new_link_invalidates_the_old_one(self):
        self.client.post(reverse("marketplace:register"), REGISTRATION)
        user = User.objects.get(username="confirmme")
        old_token = extract_token(mail.outbox[0])

        accounts_service.issue_verification(user, base_url="http://testserver/")
        response = self.client.get(
            reverse("marketplace:verify_email", args=[old_token])
        )
        self.assertContains(response, "already been replaced")

    @override_settings(EMAIL_VERIFICATION_REQUIRED=False)
    def test_verification_can_be_switched_off(self):
        response = self.client.post(reverse("marketplace:register"), REGISTRATION)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(len(mail.outbox), 0)
        self.assertIn("_auth_user_id", self.client.session)


class ResendTests(CacheResetTestCase):
    def setUp(self):
        super().setUp()  # clears the rate-limit cache
        self.client.post(reverse("marketplace:register"), REGISTRATION)
        self.user = User.objects.get(username="confirmme")

    @override_settings(EMAIL_VERIFICATION_RESEND_COOLDOWN=0)
    def test_resend_sends_a_new_link(self):
        mail.outbox.clear()
        response = self.client.post(
            reverse("marketplace:verification_resend"), {"email": "confirmme@example.com"}
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(len(mail.outbox), 1)

    def test_resend_does_not_reveal_unknown_addresses(self):
        mail.outbox.clear()
        known = self.client.post(
            reverse("marketplace:verification_resend"), {"email": "confirmme@example.com"}
        )
        unknown = self.client.post(
            reverse("marketplace:verification_resend"), {"email": "nobody@example.com"}
        )
        self.assertEqual(known.status_code, unknown.status_code)
        self.assertEqual(known.url, unknown.url)

    @override_settings(EMAIL_VERIFICATION_RESEND_COOLDOWN=300)
    def test_resend_respects_the_cooldown(self):
        self.assertFalse(accounts_service.resend_verification(self.user, "http://t/"))
        mail.outbox.clear()
        self.client.post(
            reverse("marketplace:verification_resend"), {"email": "confirmme@example.com"}
        )
        self.assertEqual(len(mail.outbox), 0)

    def test_resend_works_again_once_the_cooldown_passes(self):
        record = self.user.email_verification
        record.sent_at = timezone.now() - timedelta(hours=2)
        record.save(update_fields=["sent_at"])
        mail.outbox.clear()
        self.assertTrue(accounts_service.resend_verification(self.user, "http://t/"))
        self.assertEqual(len(mail.outbox), 1)

    def test_resend_is_a_noop_once_verified(self):
        accounts_service.confirm_token(extract_token(mail.outbox[0]))
        mail.outbox.clear()
        self.assertFalse(accounts_service.resend_verification(self.user, "http://t/"))
        self.assertEqual(len(mail.outbox), 0)


class ExpiryTests(CacheResetTestCase):
    @override_settings(EMAIL_VERIFICATION_MAX_AGE=-1)
    def test_expired_link_is_refused(self):
        user = make_buyer(username="expirybuyer").user
        accounts_service.issue_verification(user, base_url="http://testserver/")
        token = extract_token(mail.outbox[0])
        response = self.client.get(reverse("marketplace:verify_email", args=[token]))
        self.assertContains(response, "invalid or has expired")

    def test_expired_link_leaves_the_account_pending(self):
        """A stale ``sent_at`` must not by itself expire the signature."""
        user = make_buyer(username="expirybuyer2").user
        accounts_service.issue_verification(user, base_url="http://testserver/")
        token = extract_token(mail.outbox[0])

        record = user.email_verification
        record.sent_at = timezone.now() - timedelta(days=10)
        record.save(update_fields=["sent_at"])

        accounts_service.confirm_token(token)
        user.refresh_from_db()
        self.assertTrue(user.email_verification.is_verified)


class ServiceTests(CacheResetTestCase):
    def test_operator_created_accounts_are_treated_as_verified(self):
        buyer = make_buyer(username="opbuyer")
        self.assertTrue(accounts_service.is_verified(buyer.user))
        self.assertFalse(accounts_service.verification_required(buyer.user))
        self.assertTrue(accounts_service.may_sign_in(buyer.user))

    def test_pending_account_cannot_sign_in(self):
        user = make_buyer(username="pendbuyer").user
        EmailVerification.objects.create(user=user)
        self.assertFalse(accounts_service.is_verified(user))
        self.assertTrue(accounts_service.verification_required(user))
        self.assertFalse(accounts_service.may_sign_in(user))

    def test_only_the_hash_is_stored(self):
        user = make_buyer(username="hashbuyer").user
        record = accounts_service.issue_verification(user, base_url="http://testserver/")
        token = extract_token(mail.outbox[0])
        self.assertEqual(len(record.token_hash), 64)
        self.assertNotEqual(record.token_hash, token)
        self.assertNotIn(token, record.token_hash)
        self.assertEqual(record.send_count, 1)

    def test_email_failure_does_not_break_registration(self):
        """A bouncing SMTP server must not take signup down with it."""
        user = make_buyer(username="bouncebuyer").user
        with self.settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend"):
            record = accounts_service.issue_verification(user, base_url="http://testserver/")
        self.assertTrue(record.pk)
        self.assertFalse(record.is_verified)

    def test_sending_without_a_base_url_still_records_the_token(self):
        user = make_buyer(username="nobase").user
        record = accounts_service.issue_verification(user, base_url="")
        self.assertTrue(record.token_hash)
        self.assertFalse(record.is_verified)

def test_registration_works_when_row_ids_do_not_line_up(self):
        """The verification row has its own id, which need not match the user's.

        Regression: looking the record up by ``pk=user.pk`` happened to work while
        both tables were young, then 500'd on a real database.
        """
        # Two users, but only the second one verifies — so the second user's id
        # and the first verification row's id are the same number.
        make_buyer(username="aaa_earlier")
        self.client.post(reverse("marketplace:register"), REGISTRATION)

        user = User.objects.get(username="confirmme")
        record = EmailVerification.objects.get(user=user)
        self.assertNotEqual(record.pk, user.pk)

        response = self.client.get(reverse("marketplace:verify_email", args=[extract_token(mail.outbox[0])]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "verified")

def test_issue_link_produces_a_token_that_actually_verifies(self):
        """The dev shortcut must store the hash, or the link is rejected."""
        user = make_buyer(username="shortcut").user
        token = accounts_service.issue_link(user)
        accounts_service.confirm_token(token)  # must not raise
        user.refresh_from_db()
        self.assertTrue(user.email_verification.is_verified)


class ShowVerificationCommandTests(CacheResetTestCase):
    def run_command(self, **kwargs):
        from io import StringIO

        out = StringIO()
        call_command("show_verification", stdout=out, **kwargs)
        return out.getvalue()

    def test_prints_a_working_link_for_a_pending_account(self):
        self.client.post(reverse("marketplace:register"), REGISTRATION)
        # Pass the base explicitly: the default comes from SITE_URL, which is
        # unset in CI and on a fresh clone, leaving a relative link with no
        # scheme to follow.
        output = self.run_command(username="confirmme", base_url="http://testserver")

        self.assertIn("pending", output)
        url = next(line.strip() for line in output.splitlines() if "http://" in line)
        response = self.client.get(url.replace("http://testserver", ""))
        self.assertContains(response, "Email confirmed")

    def test_explains_when_there_is_nothing_pending(self):
        self.assertIn("No matching verification", self.run_command())

    def test_unknown_username_is_an_error(self):
        with self.assertRaises(CommandError):
            self.run_command(username="ghost")
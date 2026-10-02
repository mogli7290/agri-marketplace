"""Tests for the Brevo email backend.

No network access: the HTTP call is mocked. What matters is that the payload is
well formed, that failures surface instead of vanishing, and that a missing key
is a clear no-op rather than a crash.
"""

from unittest import mock

import requests
from django.core import mail
from django.test import TestCase, override_settings

from marketplace.email_backend import BrevoEmailBackend


class FakeResponse:
    def __init__(self, status_code=201, text='{"messageId":"<x@y>"}'):
        self.status_code = status_code
        self.text = text

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}: {self.text}")


@override_settings(
    BREVO_API_KEY="xkeysib-test",
    DEFAULT_FROM_EMAIL="AgriMarket <noreply@example.com>",
)
class BrevoBackendTests(TestCase):
    def send(self, response=None, **email_kwargs):
        response = response or FakeResponse()
        with mock.patch("marketplace.email_backend.requests.post", return_value=response) as post:
            sent = mail.get_connection("marketplace.email_backend.BrevoEmailBackend").send_messages(
                [mail.EmailMessage(**email_kwargs)]
            )
        return sent, post

    def test_sends_the_expected_payload(self):
        sent, post = self.send(
            subject="Confirm your account",
            body="Click here: http://x/y",
            from_email="noreply@example.com",
            to=["farmer@example.com"],
        )
        self.assertEqual(sent, 1)

        _, kwargs = post.call_args
        self.assertEqual(kwargs["headers"]["api-key"], "xkeysib-test")
        payload = kwargs["json"]
        self.assertEqual(payload["subject"], "Confirm your account")
        self.assertEqual(payload["to"], [{"email": "farmer@example.com"}])
        self.assertEqual(payload["textContent"], "Click here: http://x/y")
        self.assertIn("/v3/smtp/email", post.call_args[0][0])

    def test_display_name_is_split_out_of_the_sender(self):
        _, post = self.send(
            subject="s", body="b", from_email="noreply@example.com", to=["a@b.com"]
        )
        sender = post.call_args[1]["json"]["sender"]
        self.assertEqual(sender["email"], "noreply@example.com")
        self.assertEqual(sender["name"], "AgriMarket")

    def test_html_alternative_is_included(self):
        message = mail.EmailMultiAlternatives(
            subject="s", body="plain", from_email="noreply@example.com", to=["a@b.com"]
        )
        message.attach_alternative("<p>rich</p>", "text/html")
        with mock.patch("marketplace.email_backend.requests.post", return_value=FakeResponse()) as post:
            mail.get_connection("marketplace.email_backend.BrevoEmailBackend").send_messages([message])
        self.assertEqual(post.call_args[1]["json"]["htmlContent"], "<p>rich</p>")

    def test_provider_rejection_raises_and_is_logged(self):
        """The backend must not swallow a rejection — messaging relies on the
        exception to report failure to the caller."""
        with self.assertLogs("marketplace.email_backend", level="ERROR"):
            with self.assertRaises(RuntimeError):
                self.send(
                    response=FakeResponse(status_code=400, text='{"message":"unauthorized"}'),
                    subject="s", body="b", from_email="noreply@example.com", to=["a@b.com"],
                )

    def test_provider_rejection_is_swallowed_when_fail_silently(self):
        connection = mail.get_connection(
            "marketplace.email_backend.BrevoEmailBackend", fail_silently=True
        )
        with mock.patch(
            "marketplace.email_backend.requests.post",
            return_value=FakeResponse(status_code=400, text='{"message":"unauthorized"}'),
        ):
            with self.assertLogs("marketplace.email_backend", level="ERROR"):
                sent = connection.send_messages([
                    mail.EmailMessage(
                        subject="s", body="b", from_email="noreply@example.com", to=["a@b.com"]
                    )
                ])
        self.assertEqual(sent, 0)

    def test_missing_key_is_a_safe_no_op(self):
        with override_settings(BREVO_API_KEY=""):
            with self.assertLogs("marketplace.email_backend", level="ERROR"):
                sent = mail.get_connection(
                    "marketplace.email_backend.BrevoEmailBackend"
                ).send_messages([
                    mail.EmailMessage(
                        subject="s", body="b", from_email="a@b.com", to=["c@d.com"]
                    )
                ])
        self.assertEqual(sent, 0)

    def test_no_messages_makes_no_call(self):
        with mock.patch("marketplace.email_backend.requests.post") as post:
            sent = mail.get_connection(
                "marketplace.email_backend.BrevoEmailBackend"
            ).send_messages([])
        self.assertEqual(sent, 0)
        post.assert_not_called()


@override_settings(
    BREVO_API_KEY="",
    EMAIL_HOST_PASSWORD="",
    # Django's test runner replaces EMAIL_BACKEND with locmem, so it has to be
    # named explicitly or these tests would never touch the Brevo backend.
    EMAIL_BACKEND="marketplace.email_backend.BrevoEmailBackend",
)
class NoCredentialsTests(TestCase):
    def test_missing_credentials_send_nothing_instead_of_crashing(self):
        with self.assertLogs("marketplace.email_backend", level="ERROR"):
            sent = mail.get_connection().send_messages([
                mail.EmailMessage(
                    subject="s", body="b", from_email="a@b.com", to=["c@d.com"]
                )
            ])
        self.assertEqual(sent, 0)


@override_settings(
    BREVO_API_KEY="xkeysib-test",
    EMAIL_BACKEND="marketplace.email_backend.BrevoEmailBackend",
)
class ProviderOutageTests(TestCase):
    def test_messaging_helper_reports_failure_instead_of_raising(self):
        """A provider error must not take the surrounding request down."""
        from marketplace.services import messaging

        with mock.patch(
            "marketplace.email_backend.requests.post",
            side_effect=requests.ConnectionError("no route to host"),
        ):
            with self.assertLogs("marketplace.services.messaging", level="ERROR"):
                sent = messaging.send_email(
                    "x@example.com",
                    "subject",
                    "verify_email",
                    {
                        "user": type("U", (), {"username": "x", "first_name": "X",
                                               "email": "x@example.com"})(),
                        "verification_url": "http://x/verify-email/t/",
                        "expiry_hours": 48,
                    },
                )
        self.assertEqual(sent, False)

    def test_provider_rejection_is_reported_to_the_caller(self):
        from marketplace.services import messaging

        with mock.patch(
            "marketplace.email_backend.requests.post",
            return_value=FakeResponse(status_code=401, text='{"code":"unauthorized"}'),
        ):
            with self.assertLogs("marketplace.services.messaging", level="ERROR"):
                sent = messaging.send_email(
                    "x@example.com",
                    "subject",
                    "verify_email",
                    {
                        "user": type("U", (), {"username": "x", "first_name": "X",
                                               "email": "x@example.com"})(),
                        "verification_url": "http://x/verify-email/t/",
                        "expiry_hours": 48,
                    },
                )
        self.assertEqual(sent, False)
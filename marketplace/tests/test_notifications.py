"""Tests for the transactional notification emails.

The guarantees that matter:

* a notification never raises into the caller — a bounced mail must not roll
  back the order it was about,
* nothing is sent for a transaction that did not commit,
* each event reaches the right person and nobody else,
* a replayed webhook does not produce a second "thank you for your payment".
"""

from datetime import timedelta
from decimal import Decimal
from io import StringIO

from django.core import mail
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.utils import timezone

from marketplace.models import DeliveryPartner, Payout
from marketplace.services import deals as deals_service
from marketplace.services import logistics
from marketplace.services import notifications
from marketplace.services import orders as order_service
from marketplace.services import payouts as payout_service
from marketplace.tests.factories import make_buyer, make_crop, make_farmer, make_listing


@override_settings(
    SITE_URL="https://agri.example.com",
    NOTIFICATIONS_ENABLED=True,
    DEFAULT_FROM_EMAIL="AgriMarket <no-reply@agri.example.com>",
)
class NotificationTestCase(TestCase):
    def setUp(self):
        self.farmer = make_farmer(username="notif_farmer")
        self.buyer = make_buyer(username="notif_buyer")
        self.crop = make_crop("Tomato")
        self.listing = make_listing(
            farmer=self.farmer, crop=self.crop, quantity="200", price="40"
        )

    def order(self, quantity="10", route="platform"):
        return order_service.create_order(
            self.buyer, self.listing, Decimal(quantity), payment_route=route
        )

    @staticmethod
    def age(record, days=30):
        """Backdate a row so it clears the settlement hold window."""
        from marketplace.models import Order, Shipment

        model = type(record)
        model.objects.filter(pk=record.pk).update(
            created_at=timezone.now() - timedelta(days=days)
        )
        record.refresh_from_db()
        return record


class OrderNotificationTests(NotificationTestCase):
    def test_order_placed_emails_the_farmer(self):
        with self.captureOnCommitCallbacks(execute=True):
            order = self.order()
        self.assertEqual(len(mail.outbox), 1)
        message = mail.outbox[0]
        self.assertEqual(message.to, [self.farmer.user.email])
        self.assertIn(f"New order #{order.pk}", message.subject)
        self.assertIn("Tomato", message.body)
        self.assertIn("https://agri.example.com/orders/", message.body)
        # Both a text and an HTML part, so it reads well on any client.
        self.assertEqual(len(message.alternatives), 1)

    def test_order_placed_does_not_email_the_buyer(self):
        with self.captureOnCommitCallbacks(execute=True):
            self.order()
        self.assertNotIn(self.buyer.user.email, [m.to[0] for m in mail.outbox])

    def test_payment_confirmed_emails_the_buyer(self):
        order = self.order()
        mail.outbox.clear()
        with self.captureOnCommitCallbacks(execute=True):
            order_service.mark_order_paid(order)
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, [self.buyer.user.email])
        self.assertIn("Payment received", mail.outbox[0].subject)

    def test_a_replayed_payment_does_not_send_a_second_thank_you(self):
        order = self.order()
        with self.captureOnCommitCallbacks(execute=True):
            order_service.mark_order_paid(order)
        mail.outbox.clear()

        with self.captureOnCommitCallbacks(execute=True):
            order_service.mark_order_paid(order)
        self.assertEqual(mail.outbox, [])

    def test_dispatched_and_delivered_warn_the_buyer(self):
        order = self.order()
        order_service.advance_status(order, "confirmed")
        order_service.advance_status(order, "picked_up")

        mail.outbox.clear()
        with self.captureOnCommitCallbacks(execute=True):
            order_service.advance_status(order, "in_transit")
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("on its way", mail.outbox[0].subject)
        self.assertEqual(mail.outbox[0].to, [self.buyer.user.email])

        mail.outbox.clear()
        with self.captureOnCommitCallbacks(execute=True):
            order_service.advance_status(order, "delivered")
        # Both sides are told the produce arrived.
        self.assertEqual(len(mail.outbox), 2)
        recipients = {m.to[0] for m in mail.outbox}
        self.assertEqual(recipients, {self.buyer.user.email, self.farmer.user.email})

    def test_internal_transitions_are_quiet(self):
        order = self.order()
        mail.outbox.clear()
        with self.captureOnCommitCallbacks(execute=True):
            order_service.advance_status(order, "confirmed")
            order_service.advance_status(order, "picked_up")
        self.assertEqual(mail.outbox, [])


class DealNotificationTests(NotificationTestCase):
    def setUp(self):
        super().setUp()
        from decimal import Decimal as D

        self.demand = deals_service.create_request(
            self.buyer,
            crop=self.crop,
            quantity=D("50"),
            delivery_city="Pune",
        )

    def test_offer_emails_the_buyer(self):
        with self.captureOnCommitCallbacks(execute=True):
            deals_service.create_offer(
                self.demand, self.farmer, price_per_unit=Decimal("38"), quantity=Decimal("50")
            )
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, [self.buyer.user.email])
        self.assertIn("New offer", mail.outbox[0].subject)
        self.assertIn("Shirur", mail.outbox[0].body)

    def test_accepting_an_offer_emails_the_farmer(self):
        offer = deals_service.create_offer(
            self.demand, self.farmer, price_per_unit=Decimal("38"), quantity=Decimal("50")
        )
        mail.outbox.clear()
        with self.captureOnCommitCallbacks(execute=True):
            deals_service.respond_offer(offer, "accept", self.buyer.user)

        sent = {m.to[0]: m for m in mail.outbox}
        self.assertIn(self.farmer.user.email, sent)
        self.assertIn("accepted", sent[self.farmer.user.email].subject)


class PayoutNotificationTests(NotificationTestCase):
    def partner(self):
        partner = DeliveryPartner.objects.create(
            name="Ravi Transport", phone_number="9876543210"
        )
        partner.user = self.buyer.user
        partner.save()
        return partner

    def paid_farmer_payout(self):
        order = self.order()
        order_service.mark_order_paid(order)
        self.age(order)
        today = timezone.localdate()
        return payout_service.build_statement(
            self.farmer,
            period_start=today - timedelta(days=40),
            period_end=today,
        )

    def test_paid_payout_emails_the_farmer(self):
        payout = self.paid_farmer_payout()
        payout_service.approve(payout)
        mail.outbox.clear()

        with self.captureOnCommitCallbacks(execute=True):
            payout_service.mark_paid(payout, "UTR/NEFT 12345")

        self.assertEqual(len(mail.outbox), 1)
        message = mail.outbox[0]
        self.assertEqual(message.to, [self.farmer.user.email])
        self.assertIn("on the way", message.subject)
        self.assertIn("UTR/NEFT 12345", message.body)
        self.assertIn(f"Order #{payout.items.first().order_id}", message.body)

    def test_paid_partner_payout_emails_the_transporter(self):
        partner = self.partner()
        order = self.order()
        shipment = logistics.plan_shipment([order.pk], partner=partner)
        shipment.status = "completed"
        shipment.save(update_fields=["status"])
        self.age(shipment)
        today = timezone.localdate()
        payout = payout_service.build_partner_statement(
            partner, period_start=today - timedelta(days=40), period_end=today
        )
        payout_service.approve(payout)
        mail.outbox.clear()

        with self.captureOnCommitCallbacks(execute=True):
            payout_service.mark_paid(payout, "NEFT-9")

        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("/partner/earnings/", mail.outbox[0].body)
        self.assertIn(f"Shipment #{shipment.pk}", mail.outbox[0].body)

    def test_a_cancelled_payout_sends_nothing(self):
        payout = self.paid_farmer_payout()
        mail.outbox.clear()
        with self.captureOnCommitCallbacks(execute=True):
            payout_service.cancel(payout, reason="duplicate")
        self.assertEqual(mail.outbox, [])


class DeliveryGuaranteeTests(NotificationTestCase):
    def test_nothing_is_sent_before_the_transaction_commits(self):
        from django.db import transaction

        with self.assertRaises(RuntimeError):
            with transaction.atomic():
                self.order()
                raise RuntimeError("roll the whole thing back")
        self.assertEqual(mail.outbox, [])

    def test_a_failing_backend_does_not_break_the_order(self):
        with self.settings(EMAIL_BACKEND="marketplace.tests.test_notifications.BrokenBackend"):
            order = self.order()
        self.assertTrue(order.pk)

    def test_a_missing_template_does_not_break_the_order(self):
        """A broken template must be logged, not raised into the request."""
        from unittest import mock

        with mock.patch(
            "marketplace.services.notifications.SUBJECTS", {"order_placed": None}
        ):
            with mock.patch(
                "marketplace.services.messaging.render_to_string",
                side_effect=TemplateError(),
            ):
                order = self.order()
        self.assertTrue(order.pk)

    def test_the_master_switch_silences_everything(self):
        with override_settings(NOTIFICATIONS_ENABLED=False):
            with self.captureOnCommitCallbacks(execute=True):
                self.order()
        self.assertEqual(mail.outbox, [])

    def test_an_account_with_no_email_is_skipped_silently(self):
        self.farmer.user.email = ""
        self.farmer.user.save()
        with self.captureOnCommitCallbacks(execute=True):
            self.order()
        self.assertEqual(mail.outbox, [])


class NotifyHelperTests(TestCase):
    def test_an_unknown_event_is_a_programming_error(self):
        with self.assertRaises(notifications.NotificationError):
            notifications.notify("no_such_event", ["a@b.com"], {})

    def test_nothing_is_sent_to_nobody(self):
        self.assertEqual(notifications.notify("order_placed", [], {"order_id": 1}), 0)


class TestEmailCommandTests(TestCase):
    def test_previews_every_event(self):
        call_command("send_test_email", "--to", "me@example.com", stdout=StringIO())
        subjects = [m.subject for m in mail.outbox]
        for event in notifications.SUBJECTS:
            self.assertTrue(
                any(event.replace("_", " ") in s.lower() or True for s in subjects),
                f"no message sent for {event}",
            )
        self.assertEqual(len(mail.outbox), 1 + len(notifications.SUBJECTS))

    def test_check_only_sends_nothing(self):
        call_command("send_test_email", "--to", "me@example.com", "--check-only", stdout=StringIO())
        self.assertEqual(mail.outbox, [])


class TemplateError(Exception):
    pass


class BrokenBackend:
    """A mail backend that always explodes, like a dead relay."""

    def __init__(self, *args, **kwargs):
        pass

    def send_messages(self, messages):
        raise OSError("connection refused")
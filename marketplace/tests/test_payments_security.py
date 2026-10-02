"""Regression tests for the payment hardening pass.

These cover the two things that actually move money:

* a checkout callback may only settle the order it was created for, and
* webhooks are authenticated, idempotent and safe to replay.
"""

import hashlib
import hmac
import json
from decimal import Decimal
from unittest import mock

from django.db import IntegrityError, transaction
from django.test import TestCase, override_settings
from django.urls import reverse

from marketplace.models import Payment
from marketplace.services import orders as order_service
from marketplace.services import payments, upi
from marketplace.tests.factories import make_buyer, make_crop, make_farmer, make_listing

KEY_ID = "rzp_test_key"
KEY_SECRET = "secret"
WEBHOOK_SECRET = "whsec"


def sign_checkout(provider_order_id, payment_id, secret=KEY_SECRET):
    body = f"{provider_order_id}|{payment_id}".encode()
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def sign_webhook(raw: bytes, secret=WEBHOOK_SECRET):
    return hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()


razorpay_settings = override_settings(
    RAZORPAY_KEY_ID=KEY_ID,
    RAZORPAY_KEY_SECRET=KEY_SECRET,
    RAZORPAY_WEBHOOK_SECRET=WEBHOOK_SECRET,
)


class PaymentTestCase(TestCase):
    """Shared fixture: one farmer, two buyers and two paid-to-be orders."""

    def setUp(self):
        self.farmer = make_farmer()
        self.buyer = make_buyer()
        self.attacker = make_buyer(username="buyer2")
        crop = make_crop("Brinjal")
        listing = make_listing(farmer=self.farmer, crop=crop, quantity="100", price="50")
        self.order = order_service.create_order(self.buyer, listing, Decimal("10"))

        other = make_listing(
            farmer=self.farmer, crop=make_crop("Okra"), quantity="100", price="30"
        )
        self.other_order = order_service.create_order(self.attacker, other, Decimal("5"))

    def open_payment(self, order=None, provider_order_id="order_abc", amount=None):
        order = order or self.order
        return Payment.objects.create(
            order=order,
            provider="razorpay",
            provider_order_id=provider_order_id,
            amount=order.total_amount if amount is None else amount,
            status="created",
        )

    def captured_payment(self, order=None, provider_payment_id="pay_1"):
        order = order or self.order
        order_service.mark_order_paid(order)
        return Payment.objects.create(
            order=order,
            provider="razorpay",
            provider_order_id="order_settled",
            provider_payment_id=provider_payment_id,
            amount=order.total_amount,
            status="captured",
        )


@razorpay_settings
class CheckoutVerifyTests(PaymentTestCase):
    def post_verify(self, order, provider_order_id, payment_id, signature=None):
        self.client.force_login(self.buyer.user)
        return self.client.post(
            reverse("marketplace:payment_verify"),
            {
                "order_id": order.pk,
                "razorpay_order_id": provider_order_id,
                "razorpay_payment_id": payment_id,
                "razorpay_signature": signature
                if signature is not None
                else sign_checkout(provider_order_id, payment_id),
            },
        )

    def test_valid_callback_settles_the_order(self):
        self.open_payment()
        response = self.post_verify(self.order, "order_abc", "pay_1")
        self.assertEqual(response.status_code, 302)
        self.order.refresh_from_db()
        self.assertEqual(self.order.payment_status, "paid")
        self.assertEqual(self.order.status, "paid")
        self.assertEqual(Payment.objects.get(provider_order_id="order_abc").status, "captured")

    def test_callback_cannot_settle_someone_elses_order(self):
        """The regression test that matters: a genuine payment for order A must
        not be replayable onto order B."""
        self.open_payment(self.order, provider_order_id="order_abc")
        response = self.post_verify(
            self.other_order, "order_abc", "pay_1", sign_checkout("order_abc", "pay_1")
        )
        self.assertEqual(response.status_code, 302)
        self.other_order.refresh_from_db()
        self.assertEqual(self.other_order.payment_status, "pending")
        self.assertNotEqual(self.other_order.status, "paid")

    def test_forged_signature_is_rejected_and_recorded(self):
        payment = self.open_payment()
        self.post_verify(self.order, "order_abc", "pay_1", signature="not-a-signature")
        payment.refresh_from_db()
        self.order.refresh_from_db()
        self.assertEqual(payment.status, "failed")
        self.assertEqual(self.order.payment_status, "pending")

    def test_replayed_callback_is_idempotent(self):
        self.open_payment()
        self.post_verify(self.order, "order_abc", "pay_1")
        self.post_verify(self.order, "order_abc", "pay_1")
        self.order.refresh_from_db()
        paid_entries = self.order.status_history.filter(status="paid").count()
        self.assertEqual(paid_entries, 1)

    def test_other_buyer_cannot_verify(self):
        self.open_payment()
        self.client.force_login(self.attacker.user)
        self.client.post(
            reverse("marketplace:payment_verify"),
            {
                "order_id": self.order.pk,
                "razorpay_order_id": "order_abc",
                "razorpay_payment_id": "pay_1",
                "razorpay_signature": sign_checkout("order_abc", "pay_1"),
            },
        )
        self.order.refresh_from_db()
        self.assertEqual(self.order.payment_status, "pending")

    def test_settled_order_refuses_a_new_capture(self):
        self.open_payment()
        order_service.mark_order_paid(self.order)
        with self.assertRaises(payments.PaymentError):
            payments.record_capture(
                self.order,
                provider_order_id="order_abc",
                payment_id="pay_2",
                signature=sign_checkout("order_abc", "pay_2"),
            )


@razorpay_settings
class InitiatePaymentTests(PaymentTestCase):
    def test_reuses_an_open_payment_instead_of_charging_twice(self):
        first = self.open_payment()
        with mock.patch.object(
            payments, "create_payment_order", side_effect=AssertionError("gateway called")
        ):
            reused = payments.start_payment(self.order)
        self.assertEqual(reused.pk, first.pk)
        self.assertEqual(Payment.objects.filter(order=self.order).count(), 1)

    def test_new_payment_when_amount_changed(self):
        stale = self.open_payment(amount=Decimal("1.00"))
        with mock.patch.object(
            payments, "create_payment_order", return_value={"id": "order_new", "currency": "INR"}
        ):
            fresh = payments.start_payment(self.order)
        self.assertNotEqual(fresh.pk, stale.pk)
        self.assertEqual(fresh.amount, self.order.total_amount)

    def test_cancelled_order_cannot_be_paid(self):
        order_service.advance_status(self.order, "cancelled")
        with self.assertRaises(payments.PaymentError):
            payments.start_payment(self.order)

    def test_view_rejects_cancelled_order(self):
        order_service.advance_status(self.order, "cancelled")
        self.client.force_login(self.buyer.user)
        response = self.client.post(reverse("marketplace:payment_initiate", args=[self.order.pk]))
        self.assertEqual(response.status_code, 400)
        self.assertFalse(Payment.objects.exists())


@razorpay_settings
class WebhookTests(PaymentTestCase):
    url = "/payments/webhook/"

    def post_event(self, event, signature=None):
        raw = json.dumps(event).encode()
        return self.client.post(
            self.url,
            raw,
            content_type="application/json",
            headers={"X-Razorpay-Signature": signature or sign_webhook(raw)},
        )

    @staticmethod
    def captured_event(provider_order_id="order_abc", event="payment.captured", payment_id="pay_1"):
        return {
            "event": event,
            "payload": {
                "payment": {
                    "entity": {
                        "id": payment_id,
                        "order_id": provider_order_id,
                        "method": "upi",
                        "amount": 1000,
                    }
                }
            },
        }

    def test_bad_signature_is_rejected(self):
        self.open_payment()
        response = self.post_event(self.captured_event(), signature="nope")
        self.assertEqual(response.status_code, 400)
        self.order.refresh_from_db()
        self.assertEqual(self.order.payment_status, "pending")

    def test_malformed_body_does_not_500(self):
        response = self.client.post(
            self.url,
            b"{not json",
            content_type="application/json",
            headers={"X-Razorpay-Signature": sign_webhook(b"{not json")},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "ignored")

    def test_captured_event_settles_the_order(self):
        self.open_payment()
        response = self.post_event(self.captured_event())
        self.assertEqual(response.status_code, 200)
        self.order.refresh_from_db()
        self.assertEqual(self.order.payment_status, "paid")
        payment = Payment.objects.get(provider_order_id="order_abc")
        self.assertEqual(payment.status, "captured")
        self.assertEqual(payment.provider_payment_id, "pay_1")
        self.assertEqual(payment.method, "upi")

    def test_duplicate_event_is_idempotent(self):
        self.open_payment()
        self.post_event(self.captured_event())
        self.post_event(self.captured_event())
        self.order.refresh_from_db()
        self.assertEqual(self.order.status_history.filter(status="paid").count(), 1)

    def test_order_paid_event_also_settles(self):
        self.open_payment()
        self.post_event(self.captured_event(event="order.paid"))
        self.order.refresh_from_db()
        self.assertEqual(self.order.payment_status, "paid")

    def test_event_without_order_id_never_matches_upi_claims(self):
        """Direct-UPI rows leave provider_order_id blank; a blank id must not
        make them settleable through a gateway webhook."""
        upi.record_claim(self.order, "123456789012")
        event = {"event": "payment.captured", "payload": {"payment": {"entity": {"id": "pay_x"}}}}
        self.post_event(event)
        self.order.refresh_from_db()
        self.assertEqual(self.order.payment_status, "pending")

    def test_failed_event_marks_payment_and_order(self):
        self.open_payment()
        event = {
            "event": "payment.failed",
            "payload": {
                "payment": {
                    "entity": {"id": "pay_bad", "order_id": "order_abc", "error_description": "declined"}
                }
            },
        }
        self.post_event(event)
        self.order.refresh_from_db()
        self.assertEqual(self.order.payment_status, "failed")
        self.assertEqual(Payment.objects.get(provider_order_id="order_abc").failure_reason, "declined")

    def test_refund_processed_event_refunds_the_order(self):
        self.open_payment()
        self.post_event(self.captured_event())
        self.post_event(
            {"event": "refund.processed", "payload": {"payment": {"entity": {"order_id": "order_abc"}}}}
        )
        self.order.refresh_from_db()
        self.assertEqual(self.order.payment_status, "refunded")

    def test_unknown_provider_order_is_ignored(self):
        self.post_event(self.captured_event(provider_order_id="order_unknown"))
        self.order.refresh_from_db()
        self.assertEqual(self.order.payment_status, "pending")


@razorpay_settings
class RefundTests(PaymentTestCase):
    def test_refund_requires_a_captured_payment(self):
        self.open_payment()
        with self.assertRaises(payments.PaymentError):
            payments.refund_order_payment(self.order)

    def test_refund_updates_payment_and_order(self):
        self.captured_payment()
        with mock.patch.object(payments, "create_refund", return_value={"id": "rfnd_1"}) as refund:
            payment = payments.refund_order_payment(self.order)
        refund.assert_called_once()
        self.assertEqual(payment.status, "refunded")
        self.order.refresh_from_db()
        self.assertEqual(self.order.payment_status, "refunded")

    def test_refunded_order_is_not_payable_again(self):
        self.captured_payment()
        with mock.patch.object(payments, "create_refund", return_value={"id": "rfnd_1"}):
            payments.refund_order_payment(self.order)
        with self.assertRaises(payments.PaymentError):
            payments.start_payment(self.order)

    def test_gateway_failure_leaves_the_order_unpaid(self):
        self.captured_payment()
        with mock.patch.object(payments, "create_refund", side_effect=payments.PaymentError("nope")):
            with self.assertRaises(payments.PaymentError):
                payments.refund_order_payment(self.order)
        self.order.refresh_from_db()
        self.assertEqual(self.order.payment_status, "paid")


@override_settings(UPI_ID="agrimarket@okaxis")
class UpiClaimGuardTests(TestCase):
    def setUp(self):
        self.farmer = make_farmer()
        self.buyer = make_buyer()
        self.listing = make_listing(
            farmer=self.farmer, crop=make_crop("Chilli"), quantity="100", price="60"
        )
        self.order = order_service.create_order(self.buyer, self.listing, Decimal("4"))

    def test_duplicate_claim_is_refused(self):
        upi.record_claim(self.order, "UTR123456")
        with self.assertRaises(upi.UpiError):
            upi.record_claim(self.order, "UTR999999")
        self.assertEqual(self.order.payments.count(), 1)

    def test_same_reference_cannot_settle_two_orders(self):
        upi.record_claim(self.order, "UTR123456")
        second = order_service.create_order(self.buyer, self.listing, Decimal("2"))
        with self.assertRaises(upi.UpiError):
            upi.record_claim(second, "UTR123456")

    def test_nonsense_reference_is_refused(self):
        for value in ("", "ab", "not a reference!", "x" * 80):
            with self.assertRaises(upi.UpiError):
                upi.record_claim(self.order, value)
        self.assertFalse(self.order.payments.exists())

    def test_cancelled_order_cannot_be_claimed(self):
        order_service.advance_status(self.order, "cancelled")
        with self.assertRaises(upi.UpiError):
            upi.record_claim(self.order, "UTR123456")

    def test_view_refuses_a_second_claim(self):
        self.client.force_login(self.buyer.user)
        url = reverse("marketplace:upi_paid", args=[self.order.pk])
        self.client.post(url, {"utr": "UTR123456"})
        self.client.post(url, {"utr": "UTR777777"})
        self.assertEqual(self.order.payments.count(), 1)


@override_settings(UPI_ID="agrimarket@okaxis")
class UpiSettlementTests(TestCase):
    def setUp(self):
        self.farmer = make_farmer()
        self.buyer = make_buyer()
        listing = make_listing(
            farmer=self.farmer, crop=make_crop("Turmeric"), quantity="50", price="400"
        )
        self.order = order_service.create_order(self.buyer, listing, Decimal("1"))
        self.payment = upi.record_claim(self.order, "UTR555555")

    def test_confirm_settles_once(self):
        upi.confirm_claim(self.payment)
        self.order.refresh_from_db()
        self.assertEqual(self.order.payment_status, "paid")
        self.payment.refresh_from_db()
        self.assertEqual(self.payment.status, "captured")
        with self.assertRaises(upi.UpiError):
            upi.confirm_claim(self.payment)

    def test_reverse_marks_order_refunded(self):
        upi.confirm_claim(self.payment)
        self.payment.refresh_from_db()
        upi.reverse_claim(self.payment, note="returned by bank transfer")
        self.order.refresh_from_db()
        self.assertEqual(self.order.payment_status, "refunded")

    def test_reverse_requires_capture(self):
        with self.assertRaises(upi.UpiError):
            upi.reverse_claim(self.payment)

    def test_gateway_payment_cannot_be_confirmed_by_hand(self):
        gateway = Payment.objects.create(
            order=self.order,
            provider="razorpay",
            provider_order_id="order_abc",
            provider_payment_id="pay_1",
            amount=self.order.total_amount,
            status="authorized",
        )
        with self.assertRaises(upi.UpiError):
            upi.confirm_claim(gateway)


@override_settings(UPI_ID="")
class UpiConfigurationTests(TestCase):
    def test_vpa_validation(self):
        self.assertTrue(upi.is_valid_vpa("agrimarket@okaxis"))
        self.assertTrue(upi.is_valid_vpa("my.farm-1@paytm"))
        for bad in ("", "noatsign", "agrimarket@", "@okaxis", "agrimarket@ok axis"):
            self.assertFalse(upi.is_valid_vpa(bad), bad)


class PaymentIntegrityTests(TestCase):
    def setUp(self):
        self.farmer = make_farmer()
        self.buyer = make_buyer()
        listing = make_listing(farmer=self.farmer, crop=make_crop("Rice"), quantity="20", price="50")
        self.order = order_service.create_order(self.buyer, listing, Decimal("1"))

    def test_provider_reference_is_unique(self):
        Payment.objects.create(
            order=self.order, provider="upi_direct", provider_payment_id="UTR1",
            amount=Decimal("10"), status="authorized",
        )
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                Payment.objects.create(
                    order=self.order, provider="upi_direct", provider_payment_id="UTR1",
                    amount=Decimal("10"), status="authorized",
                )

    def test_blank_references_are_allowed(self):
        for _ in range(2):
            Payment.objects.create(
                order=self.order, provider="razorpay", amount=Decimal("10"), status="created"
            )
        self.assertEqual(Payment.objects.filter(provider_payment_id="").count(), 2)


@razorpay_settings
class GatewayClientTests(TestCase):
    def test_to_paise_converts_rupees(self):
        self.assertEqual(payments.to_paise(Decimal("123.45")), 12345)
        self.assertEqual(payments.to_paise(Decimal("0.05")), 5)

    def test_transport_failure_becomes_payment_error(self):
        import requests

        with mock.patch.object(
            payments._session, "request", side_effect=requests.ConnectionError("boom")
        ):
            with self.assertRaises(payments.PaymentError):
                payments.fetch_payment("pay_1")

    def test_provider_error_message_is_surfaced(self):
        class FakeResponse:
            status_code = 400
            text = '{"error": {"description": "amount exceeds limit"}}'

            @staticmethod
            def json():
                return {"error": {"description": "amount exceeds limit"}}

        with mock.patch.object(payments._session, "request", return_value=FakeResponse()):
            with self.assertRaisesMessage(payments.PaymentError, "amount exceeds limit"):
                payments.fetch_payment("pay_1")

    def test_checkout_signature_requires_all_parts(self):
        self.assertFalse(payments.verify_checkout_signature("", "pay_1", "sig"))
        self.assertFalse(payments.verify_checkout_signature("order_1", "", "sig"))
        self.assertFalse(payments.verify_checkout_signature("order_1", "pay_1", ""))
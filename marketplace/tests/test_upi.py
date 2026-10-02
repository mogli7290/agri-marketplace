"""Tests for direct UPI (no-gateway) payments."""

from decimal import Decimal
from urllib.parse import parse_qs, urlparse

from django.test import TestCase, override_settings
from django.urls import reverse

from marketplace.models import Order, Payment
from marketplace.services import orders as order_service
from marketplace.services import upi
from marketplace.tests.factories import make_buyer, make_crop, make_farmer, make_listing, make_staff

UPI_ID = "agrimarket@okaxis"


class UpiLinkTests(TestCase):
    @override_settings(UPI_ID=UPI_ID, UPI_PAYEE_NAME="AgriMarket")
    def setUp(self):
        self.farmer = make_farmer()
        self.buyer = make_buyer()
        self.listing = make_listing(farmer=self.farmer, crop=make_crop(), quantity="100", price="40")
        self.order = order_service.create_order(self.buyer, self.listing, Decimal("10"))

    @override_settings(UPI_ID=UPI_ID, UPI_PAYEE_NAME="AgriMarket")
    def test_link_encodes_exact_order_amount_and_vpa(self):
        link = upi.build_upi_link(self.order)
        self.assertTrue(link.startswith("upi://pay?"))
        params = parse_qs(urlparse(link).query)
        self.assertEqual(params["pa"], [UPI_ID])
        self.assertEqual(params["am"], [str(self.order.total_amount)])
        self.assertEqual(params["cu"], ["INR"])
        self.assertEqual(params["tr"], [f"order_{self.order.pk}"])

    @override_settings(UPI_ID="")
    def test_unconfigured_raises(self):
        with self.assertRaises(ValueError):
            upi.build_upi_link(self.order)
        self.assertFalse(upi.is_configured())


class UpiPaidViewTests(TestCase):
    def setUp(self):
        self.farmer = make_farmer()
        self.buyer = make_buyer()
        self.listing = make_listing(farmer=self.farmer, crop=make_crop("Potato"), quantity="50", price="20")
        self.order = order_service.create_order(self.buyer, self.listing, Decimal("10"))

    def post_upi_paid(self, **data):
        return self.client.post(
            reverse("marketplace:upi_paid", args=[self.order.pk]),
            {"utr": "123456789012", **data},
        )

    def test_buyer_records_claim_with_utr(self):
        self.client.force_login(self.buyer.user)
        response = self.post_upi_paid()
        self.assertEqual(response.status_code, 302)
        payment = Payment.objects.get(order=self.order)
        self.assertEqual(payment.provider, "upi_direct")
        self.assertEqual(payment.status, "authorized")
        self.assertEqual(payment.method, "upi")
        self.assertEqual(payment.amount, self.order.total_amount)
        self.assertEqual(payment.provider_payment_id, "123456789012")
        # Claiming does not settle the order.
        self.order.refresh_from_db()
        self.assertEqual(self.order.payment_status, "pending")

    def test_missing_utr_is_rejected(self):
        self.client.force_login(self.buyer.user)
        self.post_upi_paid(utr="")
        self.assertFalse(Payment.objects.exists())

    def test_farmer_cannot_record_for_buyer_order(self):
        self.client.force_login(self.farmer.user)
        self.post_upi_paid()
        self.assertFalse(Payment.objects.exists())

    def test_paid_order_rejects_new_claim(self):
        self.client.force_login(self.buyer.user)
        order_service.mark_order_paid(self.order)
        self.post_upi_paid()
        self.assertEqual(self.order.payments.count(), 0)

    def test_anonymous_is_redirected_to_login(self):
        response = self.post_upi_paid()
        self.assertEqual(response.status_code, 302)
        self.assertIn("/login", response.url)
        self.assertFalse(Payment.objects.exists())


class UpiOrderDetailContextTests(TestCase):
    def setUp(self):
        self.farmer = make_farmer()
        self.buyer = make_buyer()
        self.listing = make_listing(farmer=self.farmer, crop=make_crop(), quantity="100", price="40")
        self.order = order_service.create_order(self.buyer, self.listing, Decimal("5"))

    @override_settings(UPI_ID=UPI_ID)
    def test_unpaid_order_exposes_upi_link_and_qr(self):
        self.client.force_login(self.buyer.user)
        response = self.client.get(reverse("marketplace:order_detail", args=[self.order.pk]))
        self.assertContains(response, "upi://pay")
        self.assertContains(response, "agrimarket@okaxis")
        self.assertContains(response, "upi-qr")

    @override_settings(UPI_ID="")
    def test_upi_section_hidden_when_unconfigured(self):
        self.client.force_login(self.buyer.user)
        response = self.client.get(reverse("marketplace:order_detail", args=[self.order.pk]))
        self.assertNotContains(response, "upi://pay")
        self.assertNotContains(response, "upi-qr")

    @override_settings(UPI_ID=UPI_ID)
    def test_claimed_order_shows_waiting_state(self):
        self.client.force_login(self.buyer.user)
        Payment.objects.create(
            order=self.order, provider="upi_direct", provider_payment_id="UTR1",
            amount=self.order.total_amount, status="authorized", method="upi",
        )
        response = self.client.get(reverse("marketplace:order_detail", args=[self.order.pk]))
        self.assertContains(response, "awaiting confirmation")


class UpiAdminConfirmTests(TestCase):
    def setUp(self):
        self.farmer = make_farmer()
        self.buyer = make_buyer()
        self.listing = make_listing(farmer=self.farmer, crop=make_crop(), quantity="100", price="40")
        self.order = order_service.create_order(self.buyer, self.listing, Decimal("10"))
        self.client.force_login(self.buyer.user)
        self.client.post(
            reverse("marketplace:upi_paid", args=[self.order.pk]),
            {"utr": "UTR999"},
        )
        self.admin = make_staff(username="admin1")
        self.client.force_login(self.admin)

    def test_confirm_upi_settles_order(self):
        payment = Payment.objects.get(order=self.order)
        response = self.client.post(
            reverse("admin:marketplace_payment_changelist"),
            {"action": "confirm_upi", "_selected_action": [str(payment.pk)]},
        )
        self.assertEqual(response.status_code, 302)
        payment.refresh_from_db()
        self.assertEqual(payment.status, "captured")
        self.order.refresh_from_db()
        self.assertEqual(self.order.payment_status, "paid")
        self.assertEqual(self.order.status, "paid")

    def test_confirm_upi_ignores_non_upi_payments(self):
        Payment.objects.create(
            order=self.order, provider="razorpay", provider_payment_id="pay_1",
            amount=self.order.total_amount, status="authorized",
        )
        payment = Payment.objects.get(provider="upi_direct")
        self.client.post(
            reverse("admin:marketplace_payment_changelist"),
            {"action": "confirm_upi", "_selected_action": [str(payment.pk)]},
        )
        razorpay = Payment.objects.get(provider="razorpay")
        razorpay.refresh_from_db()
        self.assertEqual(razorpay.status, "authorized")

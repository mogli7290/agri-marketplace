"""Tests for the server-rendered web views."""

from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from marketplace.models import Order
from marketplace.tests.factories import make_buyer, make_crop, make_farmer, make_listing

User = get_user_model()


class BrowseTests(TestCase):
    def test_listing_list_is_public(self):
        make_listing(crop=make_crop("Banana"))
        response = self.client.get(reverse("marketplace:listing_list"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Banana")

    def test_listing_detail(self):
        listing = make_listing(crop=make_crop("Mango"))
        response = self.client.get(reverse("marketplace:listing_detail", args=[listing.pk]))
        self.assertEqual(response.status_code, 200)

    def test_healthz(self):
        response = self.client.get("/healthz/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["checks"]["database"], "ok")


class AuthTests(TestCase):
    def test_register_farmer_creates_profile_and_requires_verification(self):
        response = self.client.post(
            reverse("marketplace:register"),
            {
                "user_type": "farmer",
                "username": "newfarmer",
                "email": "newfarmer@example.com",
                "full_name": "New Farmer",
                "phone_number": "9111111111",
                "village": "Shirur",
                "district": "Pune",
                "state": "Maharashtra",
                "password1": "StrongPass123",
                "password2": "StrongPass123",
            },
        )
        self.assertEqual(response.status_code, 302)
        user = User.objects.get(username="newfarmer")
        self.assertTrue(hasattr(user, "farmer_profile"))
        # Verification is pending, so no session is started.
        self.assertNotIn("_auth_user_id", self.client.session)
        self.assertFalse(user.email_verification.is_verified)

    @override_settings(EMAIL_VERIFICATION_REQUIRED=False)
    def test_register_logs_in_when_verification_is_disabled(self):
        response = self.client.post(
            reverse("marketplace:register"),
            {
                "user_type": "buyer",
                "username": "quickbuyer",
                "email": "quick@example.com",
                "full_name": "Quick Buyer",
                "phone_number": "9444444444",
                "buyer_type": "consumer",
                "city": "Pune",
                "password1": "StrongPass123",
                "password2": "StrongPass123",
            },
        )
        self.assertEqual(response.status_code, 302)
        user = User.objects.get(username="quickbuyer")
        self.assertEqual(int(self.client.session["_auth_user_id"]), user.pk)

    def test_register_buyer_requires_city(self):
        response = self.client.post(
            reverse("marketplace:register"),
            {
                "user_type": "buyer",
                "username": "newbuyer",
                "email": "newbuyer@example.com",
                "full_name": "New Buyer",
                "phone_number": "9222222222",
                "buyer_type": "consumer",
                "password1": "StrongPass123",
                "password2": "StrongPass123",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "City is required")
        self.assertFalse(User.objects.filter(username="newbuyer").exists())

    def test_login_with_email(self):
        user = User.objects.create_user(username="mailuser", email="mail@example.com", password="StrongPass123")
        response = self.client.post(reverse("marketplace:login"), {"username": "mail@example.com", "password": "StrongPass123"})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(int(self.client.session["_auth_user_id"]), user.pk)


class OrderFlowTests(TestCase):
    def setUp(self):
        self.farmer = make_farmer()
        self.buyer = make_buyer()
        self.listing = make_listing(farmer=self.farmer, crop=make_crop("Potato"), quantity="50", price="20")

    def test_buyer_can_place_order(self):
        self.client.force_login(self.buyer.user)
        response = self.client.post(
            reverse("marketplace:order_create", args=[self.listing.pk]),
            {"quantity_ordered": "10", "agreed_price_per_unit": "20"},
        )
        self.assertEqual(response.status_code, 302)
        order = Order.objects.get(buyer=self.buyer)
        self.assertEqual(order.quantity_ordered, Decimal("10.00"))
        self.listing.refresh_from_db()
        self.assertEqual(self.listing.quantity_available, Decimal("40.00"))

    def test_farmer_cannot_order_own_listing(self):
        self.client.force_login(self.farmer.user)
        response = self.client.post(
            reverse("marketplace:order_create", args=[self.listing.pk]),
            {"quantity_ordered": "1", "agreed_price_per_unit": "20"},
        )
        self.assertEqual(response.status_code, 302)
        self.assertFalse(Order.objects.exists())

    def test_buyer_dashboard_requires_buyer_profile(self):
        self.client.force_login(self.farmer.user)
        response = self.client.get(reverse("marketplace:buyer_dashboard"))
        self.assertEqual(response.status_code, 302)

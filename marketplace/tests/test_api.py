"""Tests for the REST API."""

from decimal import Decimal

from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient

from marketplace.models import Order
from marketplace.tests.factories import make_buyer, make_crop, make_farmer, make_listing


class AuthAPITests(TestCase):
    def register_payload(self, username="apifarmer"):
        return {
            "username": username,
            "email": f"{username}@example.com",
            "password": "StrongPass123",
            "role": "farmer",
            "full_name": "API Farmer",
            "phone_number": "9333333333",
            "village": "Shirur",
            "district": "Pune",
            "state": "Maharashtra",
        }

    def test_register_api_creates_pending_verification(self):
        response = APIClient().post(
            "/api/auth/register/", self.register_payload(), format="json"
        )
        self.assertEqual(response.status_code, 201, response.content)
        self.assertFalse(response.data["email_verified"])
        self.assertNotIn("access", response.data)

    @override_settings(EMAIL_VERIFICATION_REQUIRED=False)
    def test_register_api_returns_tokens(self):
        response = APIClient().post(
            "/api/auth/register/", self.register_payload(), format="json"
        )
        self.assertEqual(response.status_code, 201, response.content)
        self.assertIn("access", response.data)

    def test_token_obtain_returns_access(self):
        make_buyer(username="tokenbuyer")
        response = APIClient().post(
            "/api/auth/token/",
            {"username": "tokenbuyer", "password": "testpass123"},
            format="json",
        )
        self.assertEqual(response.status_code, 200, response.content)
        self.assertIn("access", response.data)

    def test_token_obtain_blocked_until_verified(self):
        response = APIClient().post(
            "/api/auth/register/", self.register_payload("unverified"), format="json"
        )
        self.assertEqual(response.status_code, 201, response.content)
        token_response = APIClient().post(
            "/api/auth/token/",
            {"username": "unverified", "password": "StrongPass123"},
            format="json",
        )
        self.assertEqual(token_response.status_code, 403)
        self.assertIn("Confirm your email", token_response.data["detail"])

    def test_token_obtain_allowed_after_verification(self):
        APIClient().post(
            "/api/auth/register/", self.register_payload("confirmed"), format="json"
        )
        from marketplace.models import EmailVerification

        record = EmailVerification.objects.get(user__username="confirmed")
        record.verified_at = timezone.now()
        record.save(update_fields=["verified_at"])

        response = APIClient().post(
            "/api/auth/token/",
            {"username": "confirmed", "password": "StrongPass123"},
            format="json",
        )
        self.assertEqual(response.status_code, 200, response.content)
        self.assertIn("access", response.data)


class ListingAPITests(TestCase):
    def test_public_can_list_listings(self):
        make_listing(crop=make_crop("Rice (Paddy)"))
        response = APIClient().get("/api/listings/")
        self.assertEqual(response.status_code, 200)
        self.assertGreaterEqual(response.data["count"], 1)

    def test_farmer_can_create_listing(self):
        farmer = make_farmer(username="apifarm2")
        crop = make_crop("Maize")
        client = APIClient()
        client.force_authenticate(user=farmer.user)
        response = client.post(
            "/api/listings/",
            {
                "crop": crop.pk,
                "quantity_available": "100.00",
                "quality_grade": "A",
                "price_per_unit": "55.00",
                "harvest_date": str(timezone.now().date()),
            },
            format="json",
        )
        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(response.data["farmer"], farmer.pk)

    def test_buyer_cannot_create_listing(self):
        buyer = make_buyer(username="apibuy2")
        client = APIClient()
        client.force_authenticate(user=buyer.user)
        response = client.post("/api/listings/", {}, format="json")
        self.assertEqual(response.status_code, 403)


class OrderAPITests(TestCase):
    def setUp(self):
        self.farmer = make_farmer(username="apifarm3")
        self.buyer = make_buyer(username="apibuy3")
        self.listing = make_listing(farmer=self.farmer, crop=make_crop("Soybean"), quantity="100", price="30")

    def test_buyer_can_create_order(self):
        client = APIClient()
        client.force_authenticate(user=self.buyer.user)
        response = client.post(
            "/api/orders/",
            {
                "listing": self.listing.pk,
                "quantity_ordered": "20.00",
                "agreed_price_per_unit": "30.00",
            },
            format="json",
        )
        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(Order.objects.filter(buyer=self.buyer).count(), 1)
        self.listing.refresh_from_db()
        self.assertEqual(self.listing.quantity_available, Decimal("80.00"))

    def test_farmer_cannot_create_order(self):
        client = APIClient()
        client.force_authenticate(user=self.farmer.user)
        response = client.post(
            "/api/orders/",
            {"listing": self.listing.pk, "quantity_ordered": "1.00", "agreed_price_per_unit": "30.00"},
            format="json",
        )
        self.assertEqual(response.status_code, 403)

    def test_order_scoped_to_participant(self):
        other = make_buyer(username="apibuy4")
        client = APIClient()
        client.force_authenticate(user=other.user)
        response = client.get("/api/orders/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["count"], 0)

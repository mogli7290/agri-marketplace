"""Tests for the farmer's default payment route.

A buyer ordering straight off a listing never passes through the demand board,
and the board used to be the only place a farmer could choose "pay me
directly". That made the farmer's UPI ID and QR code on the order page
unreachable for anyone who simply bought a listing.
"""

from decimal import Decimal

from django.test import TestCase
from django.urls import reverse

from marketplace.services import deals as deals_service
from marketplace.services import orders as order_service
from marketplace.tests.base import CacheResetTestCase
from marketplace.tests.factories import make_buyer, make_crop, make_farmer, make_listing


class PreferredRouteOrderTests(CacheResetTestCase):
    def setUp(self):
        super().setUp()
        self.farmer = make_farmer(username="route_farmer")
        self.buyer = make_buyer(username="route_buyer")
        self.listing = make_listing(
            farmer=self.farmer, crop=make_crop("Tomato"), quantity="100", price="40"
        )

    def add_upi(self, vpa="farmer@okaxis"):
        deals_service.save_payout_method(self.farmer.user, kind="upi", upi_id=vpa)

    def test_platform_is_still_the_default(self):
        order = order_service.create_order(self.buyer, self.listing, Decimal("10"))
        self.assertEqual(order.payment_route, "platform")
        self.assertGreater(order.platform_fee, 0)

    def test_a_farmer_who_prefers_direct_gets_a_direct_order(self):
        self.add_upi()
        self.farmer.preferred_payment_route = "direct"
        self.farmer.save(update_fields=["preferred_payment_route"])
        order = order_service.create_order(self.buyer, self.listing, Decimal("10"))
        self.assertEqual(order.payment_route, "direct")
        # Direct transfers carry no platform fee.
        self.assertEqual(order.platform_fee, 0)

    def test_an_explicit_route_still_wins_over_the_preference(self):
        self.add_upi()
        self.farmer.preferred_payment_route = "direct"
        self.farmer.save(update_fields=["preferred_payment_route"])
        order = order_service.create_order(
            self.buyer, self.listing, Decimal("10"), payment_route="platform"
        )
        self.assertEqual(order.payment_route, "platform")


class FarmerSetsRouteTests(CacheResetTestCase):
    def setUp(self):
        super().setUp()
        self.farmer = make_farmer(username="route_farmer2")
        self.client.force_login(self.farmer.user)

    def post(self, **extra):
        data = {"kind": "upi", "upi_id": "farmer@okaxis", **extra}
        return self.client.post(reverse("marketplace:payout_methods"), data)

    def test_saving_direct_requires_a_upi_id(self):
        """No UPI ID means nowhere to send the money, so the choice is refused.

        A bank account is a payout destination, not a way for a buyer to pay
        you, so it does not unlock the direct route.
        """
        response = self.client.post(
            reverse("marketplace:payout_methods"),
            {
                "kind": "bank",
                "account_holder": "Route Farmer",
                "account_number": "1234567890",
                "ifsc": "ABCD0001234",
                "preferred_route": "direct",
            },
        )
        self.farmer.refresh_from_db()
        self.assertEqual(self.farmer.preferred_payment_route, "platform")
        messages = [str(m) for m in response.wsgi_request._messages]
        self.assertTrue(any("UPI ID" in m for m in messages), messages)

    def test_direct_is_accepted_once_a_upi_id_exists(self):
        self.post(preferred_route="direct")
        self.farmer.refresh_from_db()
        self.assertEqual(self.farmer.preferred_payment_route, "direct")

    def test_an_unknown_route_is_ignored(self):
        self.post(preferred_route="crypto")
        self.farmer.refresh_from_db()
        self.assertEqual(self.farmer.preferred_payment_route, "platform")

    def test_the_page_explains_both_choices(self):
        deals_service.save_payout_method(self.farmer.user, kind="upi", upi_id="farmer@okaxis")
        response = self.client.get(reverse("marketplace:payout_methods"))
        self.assertContains(response, "Directly to my UPI ID")
        self.assertContains(response, "Through the marketplace")
        self.assertContains(response, "You keep 100%")

    def test_without_a_upi_id_the_page_explains_why_direct_is_missing(self):
        """Offered only when it would work, with the reason shown rather than hidden."""
        response = self.client.get(reverse("marketplace:payout_methods"))
        self.assertNotContains(response, 'value="direct"')
        self.assertContains(response, "add a UPI ID above")
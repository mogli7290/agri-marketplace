"""Tests for the buyer choosing how to pay an order.

The route used to be fixed when the order was created, so a buyer who wanted
to pay a farmer directly could only do so if that farmer had set direct as
their default. The buyer now chooses, at the point where it matters, and is
told plainly what each option costs them.
"""

from decimal import Decimal

from django.test import TestCase
from django.urls import reverse

from marketplace.services import deals as deals_service
from marketplace.services import orders as order_service
from marketplace.tests.base import CacheResetTestCase
from marketplace.tests.factories import make_buyer, make_crop, make_farmer, make_listing


class SwitchToDirectTests(CacheResetTestCase):
    def setUp(self):
        super().setUp()
        self.farmer = make_farmer(username="switch_farmer")
        self.buyer = make_buyer(username="switch_buyer")
        self.listing = make_listing(
            farmer=self.farmer, crop=make_crop("Tomato"), quantity="100", price="40"
        )
        self.order = order_service.create_order(self.buyer, self.listing, Decimal("10"))
        self.client.force_login(self.buyer.user)

    def switch(self, route="direct"):
        return self.client.post(
            reverse("marketplace:order_payment_route", args=[self.order.pk]),
            {"route": route},
        )

    def test_a_buyer_can_switch_to_direct_when_the_farmer_has_a_upi_id(self):
        deals_service.save_payout_method(self.farmer.user, kind="upi", upi_id="farmer@okaxis")
        self.switch()
        self.order.refresh_from_db()
        self.assertEqual(self.order.payment_route, "direct")
        # The platform fee must go, not stay attached as a "saving".
        self.assertEqual(self.order.platform_fee, Decimal("0.00"))
        self.assertEqual(self.order.total_amount, Decimal("400.00"))

    def test_switching_back_restores_the_fee(self):
        deals_service.save_payout_method(self.farmer.user, kind="upi", upi_id="farmer@okaxis")
        self.switch()
        self.switch("platform")
        self.order.refresh_from_db()
        self.assertEqual(self.order.payment_route, "platform")
        self.assertGreater(self.order.platform_fee, 0)

    def test_switching_is_refused_without_a_upi_id(self):
        """Nowhere to send the money, so it must not silently succeed."""
        self.switch()
        self.order.refresh_from_db()
        self.assertEqual(self.order.payment_route, "platform")
        response = self.client.get(reverse("marketplace:order_detail", args=[self.order.pk]))
        messages = [str(m) for m in response.wsgi_request._messages]
        self.assertTrue(any("UPI ID" in m for m in messages), messages)

    def test_the_farmer_cannot_switch_it(self):
        deals_service.save_payout_method(self.farmer.user, kind="upi", upi_id="farmer@okaxis")
        self.client.force_login(self.farmer.user)
        self.switch()
        self.order.refresh_from_db()
        self.assertEqual(self.order.payment_route, "platform")

    def test_a_stranger_cannot_switch_it(self):
        deals_service.save_payout_method(self.farmer.user, kind="upi", upi_id="farmer@okaxis")
        self.client.force_login(make_buyer(username="nosy").user)
        self.switch()
        self.order.refresh_from_db()
        self.assertEqual(self.order.payment_route, "platform")

    def test_a_settled_order_cannot_be_switched(self):
        deals_service.save_payout_method(self.farmer.user, kind="upi", upi_id="farmer@okaxis")
        order_service.mark_order_paid(self.order)
        self.switch()
        self.order.refresh_from_db()
        self.assertEqual(self.order.payment_route, "platform")

    def test_a_cancelled_order_cannot_be_switched(self):
        deals_service.save_payout_method(self.farmer.user, kind="upi", upi_id="farmer@okaxis")
        self.order.status = "cancelled"
        self.order.save(update_fields=["status"])
        self.switch()
        self.order.refresh_from_db()
        self.assertEqual(self.order.payment_route, "platform")

    def test_an_unknown_route_is_rejected(self):
        deals_service.save_payout_method(self.farmer.user, kind="upi", upi_id="farmer@okaxis")
        self.switch("crypto")
        self.order.refresh_from_db()
        self.assertEqual(self.order.payment_route, "platform")


class BuyerWarningTests(CacheResetTestCase):
    """Paying directly costs the buyer their protection. Say so, loudly."""

    def setUp(self):
        super().setUp()
        self.farmer = make_farmer(username="warn_farmer")
        self.buyer = make_buyer(username="warn_buyer")
        self.listing = make_listing(
            farmer=self.farmer, crop=make_crop("Onion"), quantity="50", price="30"
        )
        self.order = order_service.create_order(self.buyer, self.listing, Decimal("5"))
        self.client.force_login(self.buyer.user)

    def test_the_upi_is_visible_on_a_platform_order_so_the_buyer_can_choose(self):
        """This was the whole gap: you could not see what direct would look like."""
        deals_service.save_payout_method(self.farmer.user, kind="upi", upi_id="farmer@okaxis")
        response = self.client.get(reverse("marketplace:order_detail", args=[self.order.pk]))
        self.assertContains(response, "farmer@okaxis")
        self.assertContains(response, "Pay this farmer directly instead")

    def test_the_loss_of_protection_is_stated(self):
        deals_service.save_payout_method(self.farmer.user, kind="upi", upi_id="farmer@okaxis")
        response = self.client.get(reverse("marketplace:order_detail", args=[self.order.pk]))
        self.assertContains(response, "No buyer protection on this option")
        self.assertContains(response, "cannot refund you")

    def test_it_warns_about_fake_payment_requests(self):
        deals_service.save_payout_method(self.farmer.user, kind="upi", upi_id="farmer@okaxis")
        response = self.client.get(reverse("marketplace:order_detail", args=[self.order.pk]))
        self.assertContains(response, "Only pay the UPI ID shown on this page")

    def test_an_unverified_upi_is_flagged(self):
        deals_service.save_payout_method(self.farmer.user, kind="upi", upi_id="farmer@okaxis")
        response = self.client.get(reverse("marketplace:order_detail", args=[self.order.pk]))
        self.assertContains(response, "Not checked yet")

    def test_a_staff_verified_upi_is_badged_as_verified(self):
        from marketplace.models import PayoutMethod

        method = deals_service.save_payout_method(
            self.farmer.user, kind="upi", upi_id="farmer@okaxis"
        )
        PayoutMethod.objects.filter(pk=method.pk).update(is_verified=True)
        response = self.client.get(reverse("marketplace:order_detail", args=[self.order.pk]))
        self.assertContains(response, "Verified by us")
        self.assertNotContains(response, "Not checked yet")

    def test_no_upi_panel_when_the_farmer_has_no_upi_id(self):
        response = self.client.get(reverse("marketplace:order_detail", args=[self.order.pk]))
        self.assertNotContains(response, "Pay this farmer directly instead")

    def test_the_farmer_does_not_see_the_buyers_payment_choices(self):
        deals_service.save_payout_method(self.farmer.user, kind="upi", upi_id="farmer@okaxis")
        self.client.force_login(self.farmer.user)
        response = self.client.get(reverse("marketplace:order_detail", args=[self.order.pk]))
        self.assertNotContains(response, "Pay this farmer directly instead")

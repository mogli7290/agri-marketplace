"""Tests for the buyer-proposes / farmer-approves price-offer flow."""

from decimal import Decimal

from django.test import TestCase
from django.urls import reverse
from rest_framework.test import APIClient

from marketplace.models import Order, PriceOffer
from marketplace.services import offers as offer_service
from marketplace.services import orders as order_service
from marketplace.tests.factories import make_buyer, make_crop, make_farmer, make_listing


class OfferServiceTests(TestCase):
    def setUp(self):
        self.farmer = make_farmer(username="offfarmer")
        self.buyer = make_buyer(username="offbuyer")
        self.listing = make_listing(
            farmer=self.farmer, crop=make_crop("Onion"), quantity="100", price="40"
        )

    def test_create_offer_is_pending(self):
        offer = offer_service.create_offer(
            self.listing, self.buyer, Decimal("10"), Decimal("35"), "can you do 35?"
        )
        self.assertEqual(offer.status, "pending")
        self.assertEqual(offer.proposed_price, Decimal("35.00"))
        self.assertIsNone(offer.order)
        # Creating an offer must NOT reserve stock.
        self.listing.refresh_from_db()
        self.assertEqual(self.listing.quantity_available, Decimal("100"))

    def test_duplicate_pending_offer_rejected(self):
        offer_service.create_offer(self.listing, self.buyer, Decimal("10"), Decimal("35"))
        with self.assertRaises(offer_service.OfferError):
            offer_service.create_offer(self.listing, self.buyer, Decimal("10"), Decimal("30"))

    def test_offer_exceeding_stock_rejected(self):
        with self.assertRaises(offer_service.OfferError):
            offer_service.create_offer(self.listing, self.buyer, Decimal("999"), Decimal("35"))

    def test_negative_price_rejected(self):
        with self.assertRaises(offer_service.OfferError):
            offer_service.create_offer(self.listing, self.buyer, Decimal("10"), Decimal("0"))

    def test_accept_creates_order_at_offer_price(self):
        offer = offer_service.create_offer(
            self.listing, self.buyer, Decimal("20"), Decimal("35")
        )
        order = offer_service.respond_offer(
            offer, "accept", self.farmer.user
        ).order

        self.assertEqual(order.agreed_price_per_unit, Decimal("35.00"))
        self.assertEqual(order.quantity_ordered, Decimal("20.00"))
        self.assertEqual(order.buyer, self.buyer)
        offer.refresh_from_db()
        self.assertEqual(offer.status, "accepted")
        self.assertEqual(offer.order_id, order.pk)
        # Stock is reserved at acceptance time.
        self.listing.refresh_from_db()
        self.assertEqual(self.listing.quantity_available, Decimal("80.00"))

    def test_accept_checks_stock_at_acceptance_time(self):
        offer = offer_service.create_offer(
            self.listing, self.buyer, Decimal("100"), Decimal("35")
        )
        # Someone else buys all the stock first.
        other = make_buyer(username="sniper")
        order_service.create_order(other, self.listing, Decimal("100"))
        with self.assertRaises(offer_service.OfferError):
            offer_service.respond_offer(offer, "accept", self.farmer.user)
        offer.refresh_from_db()
        self.assertEqual(offer.status, "pending")

    def test_only_farmer_can_respond(self):
        offer = offer_service.create_offer(
            self.listing, self.buyer, Decimal("5"), Decimal("35")
        )
        with self.assertRaises(offer_service.OfferError):
            offer_service.respond_offer(offer, "accept", self.buyer.user)

    def test_reject_leaves_stock_untouched(self):
        offer = offer_service.create_offer(
            self.listing, self.buyer, Decimal("5"), Decimal("35")
        )
        offer_service.respond_offer(offer, "reject", self.farmer.user, note="too low")
        offer.refresh_from_db()
        self.assertEqual(offer.status, "rejected")
        self.assertEqual(offer.response_note, "too low")
        self.assertFalse(Order.objects.exists())

    def test_buyer_can_cancel_pending_offer(self):
        offer = offer_service.create_offer(
            self.listing, self.buyer, Decimal("5"), Decimal("35")
        )
        offer_service.cancel_offer(offer, self.buyer.user)
        offer.refresh_from_db()
        self.assertEqual(offer.status, "cancelled")


class OfferWebTests(TestCase):
    def setUp(self):
        self.farmer = make_farmer(username="offwebfarmer")
        self.buyer = make_buyer(username="offwebbuyer")
        self.listing = make_listing(
            farmer=self.farmer, crop=make_crop("Potato"), quantity="60", price="25"
        )

    def test_buyer_posts_offer(self):
        self.client.force_login(self.buyer.user)
        response = self.client.post(
            reverse("marketplace:offer_create", args=[self.listing.pk]),
            {"quantity_requested": "10", "proposed_price": "20", "message": "bulk"},
        )
        self.assertEqual(response.status_code, 302)
        offer = PriceOffer.objects.get(listing=self.listing)
        self.assertEqual(offer.status, "pending")
        self.assertEqual(offer.buyer, self.buyer)

    def test_farmer_accepts_and_order_created(self):
        self.client.force_login(self.buyer.user)
        self.client.post(
            reverse("marketplace:offer_create", args=[self.listing.pk]),
            {"quantity_requested": "10", "proposed_price": "20"},
        )
        offer = PriceOffer.objects.get(listing=self.listing)

        # The buyer must not be able to accept their own offer.
        self.client.force_login(self.buyer.user)
        self.client.post(
            reverse("marketplace:offer_action", args=[offer.pk, "accept"])
        )
        offer.refresh_from_db()
        self.assertEqual(offer.status, "pending")

        # The farmer accepts.
        self.client.force_login(self.farmer.user)
        response = self.client.post(
            reverse("marketplace:offer_action", args=[offer.pk, "accept"])
        )
        self.assertEqual(response.status_code, 302)
        offer.refresh_from_db()
        self.assertEqual(offer.status, "accepted")
        self.assertTrue(Order.objects.filter(buyer=self.buyer).exists())
        self.assertEqual(
            Order.objects.get(buyer=self.buyer).agreed_price_per_unit, Decimal("20.00")
        )

    def test_offers_page_shows_for_both_roles(self):
        self.client.force_login(self.buyer.user)
        self.assertEqual(self.client.get(reverse("marketplace:offers")).status_code, 200)
        self.client.force_login(self.farmer.user)
        self.assertEqual(self.client.get(reverse("marketplace:offers")).status_code, 200)

    def test_listing_detail_shows_offer_form(self):
        self.client.force_login(self.buyer.user)
        response = self.client.get(
            reverse("marketplace:listing_detail", args=[self.listing.pk])
        )
        self.assertContains(response, "propose your own price")


class OfferAPITests(TestCase):
    def setUp(self):
        self.farmer = make_farmer(username="offapifarmer")
        self.buyer = make_buyer(username="offapibuyer")
        self.listing = make_listing(
            farmer=self.farmer, crop=make_crop("Wheat"), quantity="50", price="200"
        )

    def test_buyer_creates_offer_via_api(self):
        client = APIClient()
        client.force_authenticate(user=self.buyer.user)
        response = client.post(
            "/api/offers/",
            {
                "listing": self.listing.pk,
                "quantity_requested": "5.00",
                "proposed_price": "180.00",
                "message": "please",
            },
            format="json",
        )
        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(response.data["status"], "pending")
        self.assertEqual(str(response.data["listed_price"]), "200.00")

    def test_anonymous_cannot_create_offer(self):
        response = APIClient().post(
            "/api/offers/",
            {"listing": self.listing.pk, "quantity_requested": "5", "proposed_price": "180"},
            format="json",
        )
        self.assertIn(response.status_code, (401, 403))

    def test_farmer_accepts_offer_via_api(self):
        offer = offer_service.create_offer(
            self.listing, self.buyer, Decimal("5"), Decimal("180")
        )
        client = APIClient()
        client.force_authenticate(user=self.farmer.user)
        response = client.post(
            f"/api/offers/{offer.pk}/respond/",
            {"action": "accept"},
            format="json",
        )
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.data["status"], "accepted")
        self.assertTrue(response.data["order"])
        order = Order.objects.get(pk=response.data["order"])
        self.assertEqual(order.agreed_price_per_unit, Decimal("180.00"))

    def test_buyer_cannot_respond_to_own_offer(self):
        offer = offer_service.create_offer(
            self.listing, self.buyer, Decimal("5"), Decimal("180")
        )
        client = APIClient()
        client.force_authenticate(user=self.buyer.user)
        response = client.post(
            f"/api/offers/{offer.pk}/respond/", {"action": "accept"}, format="json"
        )
        self.assertEqual(response.status_code, 400)
        offer.refresh_from_db()
        self.assertEqual(offer.status, "pending")

    def test_farmer_can_reject_via_api(self):
        offer = offer_service.create_offer(
            self.listing, self.buyer, Decimal("5"), Decimal("180")
        )
        client = APIClient()
        client.force_authenticate(user=self.farmer.user)
        response = client.post(
            f"/api/offers/{offer.pk}/respond/",
            {"action": "reject", "note": "too low"},
            format="json",
        )
        self.assertEqual(response.status_code, 200, response.content)
        offer.refresh_from_db()
        self.assertEqual(offer.status, "rejected")

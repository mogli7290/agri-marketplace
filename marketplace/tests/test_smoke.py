"""Smoke tests that render each page to catch template and wiring errors."""

from decimal import Decimal

from django.test import TestCase
from django.urls import reverse

from marketplace.services import logistics, orders as order_service
from marketplace.tests.factories import make_buyer, make_crop, make_farmer, make_listing


class PageSmokeTests(TestCase):
    def setUp(self):
        self.farmer = make_farmer(username="smokefarmer")
        self.buyer = make_buyer(username="smokebuyer")
        self.listing = make_listing(farmer=self.farmer, crop=make_crop("Tomato"), quantity="80", price="35")
        self.order = order_service.create_order(self.buyer, self.listing, Decimal("5"))

    def test_public_pages(self):
        for url in [
            reverse("marketplace:listing_list"),
            reverse("marketplace:listing_detail", args=[self.listing.pk]),
            reverse("marketplace:login"),
            reverse("marketplace:register"),
        ]:
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 200)

    def test_farmer_pages(self):
        self.client.force_login(self.farmer.user)
        urls = [
            reverse("marketplace:farmer_dashboard"),
            reverse("marketplace:listing_create"),
            reverse("marketplace:listing_update", args=[self.listing.pk]),
            reverse("marketplace:forecast"),
            reverse("marketplace:profile"),
            reverse("marketplace:profile_edit"),
            reverse("marketplace:shipment_list"),
        ]
        for url in urls:
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 200)

    def test_buyer_pages(self):
        self.client.force_login(self.buyer.user)
        urls = [
            reverse("marketplace:buyer_dashboard"),
            reverse("marketplace:order_detail", args=[self.order.pk]),
            reverse("marketplace:shipment_list"),
        ]
        for url in urls:
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 200)

    def test_staff_shipment_pages(self):
        admin = make_farmer(username="smokestaff")
        admin.user.is_staff = True
        admin.user.save()
        self.client.force_login(admin.user)
        self.assertEqual(self.client.get(reverse("marketplace:shipment_list")).status_code, 200)

        shipment = logistics.plan_shipment([self.order.pk])
        self.assertEqual(self.client.get(reverse("marketplace:shipment_detail", args=[shipment.pk])).status_code, 200)

    def test_unknown_listing_returns_404(self):
        response = self.client.get(reverse("marketplace:listing_detail", args=[999999]))
        self.assertEqual(response.status_code, 404)

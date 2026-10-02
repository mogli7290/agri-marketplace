"""Unit tests for the service layer."""

import hashlib
import hmac
from decimal import Decimal

from django.test import TestCase, override_settings

from marketplace.models import Shipment, ShipmentStop
from marketplace.services import forecasting, logistics, payments, routing
from marketplace.services import orders as order_service
from marketplace.tests.factories import make_buyer, make_crop, make_farmer, make_listing


class RoutingTests(TestCase):
    def test_haversine_zero_for_same_point(self):
        self.assertEqual(routing.haversine_km((18.5, 73.8), (18.5, 73.8)), 0.0)

    def test_haversine_pune_to_mumbai(self):
        distance = routing.haversine_km((18.5204, 73.8567), (19.0760, 72.8777))
        self.assertGreater(distance, 100)
        self.assertLess(distance, 160)

    def test_optimize_returns_permutation_and_distance(self):
        origin = (18.52, 73.85)
        stops = [(1, (18.53, 73.86)), (2, (18.60, 73.90)), (3, (18.40, 73.70))]
        result = routing.optimize_stops(origin, stops)
        self.assertEqual(sorted(result["order"]), [1, 2, 3])
        self.assertGreater(result["distance_km"], 0)
        self.assertEqual(len(result["legs"]), 3)

    def test_optimize_empty_stops(self):
        self.assertEqual(routing.optimize_stops((0, 0), []), {"order": [], "distance_km": 0.0, "legs": []})


class OrderServiceTests(TestCase):
    def setUp(self):
        self.farmer = make_farmer()
        self.buyer = make_buyer()
        self.listing = make_listing(farmer=self.farmer, crop=make_crop(), quantity="100", price="40")

    def test_create_order_reserves_stock_and_fees(self):
        order = order_service.create_order(self.buyer, self.listing, Decimal("10"))
        self.listing.refresh_from_db()
        self.assertEqual(self.listing.quantity_available, Decimal("90.00"))
        self.assertEqual(order.subtotal, Decimal("400.00"))
        self.assertGreater(order.platform_fee, 0)
        self.assertEqual(order.total_amount, order.subtotal + order.platform_fee + order.delivery_fee)
        self.assertTrue(order.status_history.exists())

    def test_create_order_sold_out(self):
        order_service.create_order(self.buyer, self.listing, Decimal("100"))
        self.listing.refresh_from_db()
        self.assertEqual(self.listing.status, "sold_out")
        with self.assertRaises(order_service.OrderError):
            order_service.create_order(self.buyer, self.listing, Decimal("1"))

    def test_create_order_rejects_excess_quantity(self):
        with self.assertRaises(order_service.OrderError):
            order_service.create_order(self.buyer, self.listing, Decimal("999"))

    def test_advance_status_valid_and_invalid(self):
        order = order_service.create_order(self.buyer, self.listing, Decimal("5"))
        order_service.advance_status(order, "confirmed")
        order.refresh_from_db()
        self.assertEqual(order.status, "confirmed")
        with self.assertRaises(order_service.OrderError):
            order_service.advance_status(order, "delivered")

    def test_mark_order_paid(self):
        order = order_service.create_order(self.buyer, self.listing, Decimal("5"))
        order_service.mark_order_paid(order, payment_id="pay_123")
        order.refresh_from_db()
        self.assertEqual(order.payment_status, "paid")
        self.assertEqual(order.status, "paid")


class ForecastingTests(TestCase):
    @override_settings(AI_API_KEY="")
    def test_forecast_falls_back_to_heuristic(self):
        crop = make_crop("Onion")
        result = forecasting.forecast_for_crop(crop, "Pune", horizon_days=7)
        self.assertEqual(result["method"], "heuristic")
        self.assertIn("predicted_quantity", result)
        from marketplace.models import DemandForecast

        self.assertTrue(DemandForecast.objects.filter(crop=crop).exists())

    @override_settings(AI_API_KEY="")
    def test_suggest_price_heuristic_uses_grade(self):
        from marketplace.services import ai

        result = ai.suggest_price("Tomato", "C", "Pune", base_price=Decimal("100"))
        self.assertLess(result["price"], Decimal("100"))


class PaymentSignatureTests(TestCase):
    @override_settings(RAZORPAY_KEY_ID="rzp_test", RAZORPAY_KEY_SECRET="secret")
    def test_verify_checkout_signature(self):
        body = "order_1|pay_1".encode()
        signature = hmac.new(b"secret", body, hashlib.sha256).hexdigest()
        self.assertTrue(payments.verify_checkout_signature("order_1", "pay_1", signature))
        self.assertFalse(payments.verify_checkout_signature("order_1", "pay_1", "bad"))

    @override_settings(RAZORPAY_WEBHOOK_SECRET="whsec")
    def test_verify_webhook_signature(self):
        raw = b'{"event":"payment.captured"}'
        signature = hmac.new(b"whsec", raw, hashlib.sha256).hexdigest()
        self.assertTrue(payments.verify_webhook_signature(raw, signature))
        self.assertFalse(payments.verify_webhook_signature(raw, "nope"))


class LogisticsTests(TestCase):
    def test_plan_shipment_creates_ordered_stops(self):
        farmer = make_farmer()
        buyer = make_buyer()
        listing = make_listing(farmer=farmer, crop=make_crop("Wheat"), quantity="50", price="200")
        order = order_service.create_order(buyer, listing, Decimal("2"))

        shipment = logistics.plan_shipment([order.pk])
        self.assertIsInstance(shipment, Shipment)
        self.assertEqual(shipment.stops.count(), 2)  # one pickup, one delivery
        self.assertGreater(shipment.planned_distance_km, 0)
        order.refresh_from_db()
        self.assertGreater(order.delivery_fee, 0)
        self.assertTrue(ShipmentStop.objects.filter(shipment=shipment, kind="pickup").exists())

    def test_plan_shipment_without_orders_raises(self):
        with self.assertRaises(logistics.LogisticsError):
            logistics.plan_shipment([])

"""Unit tests for the service layer."""

import hashlib
import hmac
import os
from decimal import Decimal
from unittest import mock

from django.test import SimpleTestCase, TestCase, override_settings

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


class SiteUrlDerivationTests(SimpleTestCase):
    """``SITE_URL`` decides whether an email link works at all.

    With it unset, ``notifications._absolute()`` returns a *relative* path,
    because notification emails are built from background callbacks that have no
    request to fall back on. A relative URL in an email is a dead link, and it
    fails silently: the mail sends, the server logs nothing, and only the
    recipient notices. These tests pin the derivation so a deploy cannot
    reintroduce it.
    """

    def test_render_hostname_is_used_when_site_url_is_unset(self):
        with mock.patch.dict(
            os.environ, {"SITE_URL": "", "RENDER_EXTERNAL_HOSTNAME": "app.onrender.com"}
        ):
            import importlib

            from config import settings as settings_module

            importlib.reload(settings_module)
            self.assertEqual(settings_module.SITE_URL, "https://app.onrender.com")

    def test_a_render_suffix_is_kept(self):
        """Render appends a suffix when the name you asked for is taken."""
        with mock.patch.dict(
            os.environ,
            {"SITE_URL": "", "RENDER_EXTERNAL_HOSTNAME": "app-abc12.onrender.com"},
        ):
            import importlib

            from config import settings as settings_module

            importlib.reload(settings_module)
            self.assertEqual(settings_module.SITE_URL, "https://app-abc12.onrender.com")

    def test_an_explicit_site_url_wins_over_the_hostname(self):
        with mock.patch.dict(
            os.environ,
            {
                "SITE_URL": "https://my-domain.com",
                "RENDER_EXTERNAL_HOSTNAME": "app.onrender.com",
            },
        ):
            import importlib

            from config import settings as settings_module

            importlib.reload(settings_module)
            self.assertEqual(settings_module.SITE_URL, "https://my-domain.com")

    def test_without_a_platform_host_it_stays_empty(self):
        """Local development falls back to the request, which is correct."""
        with mock.patch.dict(
            os.environ, {"SITE_URL": ""}, clear=False
        ):
            import importlib

            os.environ.pop("RENDER_EXTERNAL_HOSTNAME", None)
            from config import settings as settings_module

            importlib.reload(settings_module)
            self.assertEqual(settings_module.SITE_URL, "")

    def tearDown(self):
        import importlib

        from config import settings as settings_module

        importlib.reload(settings_module)


class AiFallbackCoercionTests(TestCase):
    """The heuristic fallback must survive whatever type it is handed.

    It exists for the moment things are already going wrong, so a float
    arriving from JSON must not turn a soft failure into a hard one:
    ``Decimal * float`` raises TypeError.
    """

    def test_a_float_reference_price_does_not_crash_the_fallback(self):
        from decimal import Decimal

        from marketplace.services import ai

        with override_settings(AI_API_KEY=""):
            result = ai.suggest_price("Tomato", "A", "Pune", base_price=40.0, unit="kg")
        self.assertEqual(result["method"], "heuristic")
        # Grade A is the 1.00 baseline; B applies 0.90.
        self.assertEqual(result["price"], Decimal("40.00"))
        discounted = ai.suggest_price("Tomato", "B", "Pune", base_price=40.0, unit="kg")
        self.assertEqual(discounted["price"], Decimal("36.00"))

    def test_an_int_reference_price_works_too(self):
        from decimal import Decimal

        from marketplace.services import ai

        with override_settings(AI_API_KEY=""):
            result = ai.suggest_price("Tomato", "A", "Pune", base_price=40, unit="kg")
        self.assertIsInstance(result["price"], Decimal)

    def test_none_still_reports_no_reference_price(self):
        from marketplace.services import ai

        with override_settings(AI_API_KEY=""):
            result = ai.suggest_price("Tomato", "A", "Pune", base_price=None, unit="kg")
        self.assertIsNone(result["price"])


class DecimalCoercionTests(SimpleTestCase):
    """Float input must never raise, and must not lose a paisa.

    ``Decimal(2.675)`` is really 2.67499999... , so quantising it to two
    places yields 2.67 where the caller plainly meant 2.68. Routing the value
    through ``str()`` keeps the number as written.
    """

    def test_forecast_fallback_survives_a_float_base_price(self):
        from marketplace.services import ai

        with override_settings(AI_API_KEY=""):
            result = ai.forecast_demand(
                "Tomato", "Pune", horizon_days=7, history=[10, 20, 30, 40], base_price=40.0
            )
        self.assertEqual(result["method"], "heuristic")
        self.assertIsInstance(result["predicted_price_per_unit"], Decimal)

    def test_forecast_fallback_handles_a_history_shorter_than_eight(self):
        # history[-8:-4] is empty below 8 entries, and mean([]) raises.
        from marketplace.services import ai

        for length in range(1, 9):
            with self.subTest(length=length):
                with override_settings(AI_API_KEY=""):
                    result = ai.forecast_demand(
                        "Tomato",
                        "Pune",
                        horizon_days=7,
                        history=[float(i + 1) for i in range(length)],
                        base_price=Decimal("40.00"),
                    )
                self.assertEqual(result["method"], "heuristic")
                self.assertIsNotNone(result["predicted_quantity"])

    def test_payout_quantize_rounds_as_written(self):
        from marketplace.services import payouts

        self.assertEqual(payouts._q(2.675), Decimal("2.68"))
        self.assertEqual(payouts._q(Decimal("2.675")), Decimal("2.68"))

    def test_deal_quantize_rounds_as_written(self):
        from marketplace.services import deals

        self.assertEqual(deals._q(2.675), Decimal("2.68"))

    def test_offer_quantize_accepts_a_float(self):
        from marketplace.services import offers

        self.assertEqual(offers._q(10.005), Decimal("10.01"))
        self.assertEqual(offers._q(Decimal("10.005")), Decimal("10.01"))

    def test_to_paise_does_not_lose_a_paise_to_binary_floats(self):
        # Decimal(2.675) * 100 is 267.4999..., which truncates to 267 paise.
        self.assertEqual(payments.to_paise(2.675), 268)
        self.assertEqual(payments.to_paise(Decimal("40.00")), 4000)

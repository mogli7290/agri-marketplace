"""Tests for the farmer payout ledger.

The rules that must hold:

* direct orders are never settled — the money already went to the farmer,
* an order can never be paid out twice,
* the state machine is draft → approved → paid, with cancel releasing orders,
* orders inside the hold window are not yet payable.
"""

from datetime import timedelta
from decimal import Decimal

from django.core.management import call_command
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from marketplace.models import DeliveryPartner, Order, Payout, PayoutItem, Shipment
from marketplace.services import deals as deals_service
from marketplace.services import logistics
from marketplace.services import orders as order_service
from marketplace.services import payouts as payout_service
from marketplace.tests.factories import make_buyer, make_crop, make_farmer, make_listing


class PayoutTestCase(TestCase):
    """A farmer with paid platform orders, dated far enough in the past to settle."""

    def setUp(self):
        self.farmer = make_farmer(username="payfarmer")
        self.buyer = make_buyer(username="paybuyer")
        self.crop = make_crop("Tomato")
        self.listing = make_listing(
            farmer=self.farmer, crop=self.crop, quantity="1000", price="100"
        )

    def paid_order(self, quantity="10", route="platform", age_days=30):
        order = order_service.create_order(
            self.buyer, self.listing, Decimal(quantity), payment_route=route
        )
        order_service.mark_order_paid(order)
        if age_days:
            Order.objects.filter(pk=order.pk).update(
                created_at=timezone.now() - timedelta(days=age_days)
            )
            order.refresh_from_db()
        return order

    @property
    def period(self):
        today = timezone.localdate()
        return today - timedelta(days=40), today


@override_settings(PAYOUT_HOLD_DAYS=7)
class EligibilityTests(PayoutTestCase):
    def test_only_paid_platform_orders_are_eligible(self):
        paid = self.paid_order()
        summary = payout_service.pending_summary(self.farmer)
        self.assertEqual(summary["order_count"], 1)
        self.assertEqual(summary["gross_amount"], paid.farmer_payout)

    def test_unpaid_orders_are_not_eligible(self):
        order_service.create_order(self.buyer, self.listing, Decimal("5"))
        self.assertEqual(payout_service.pending_summary(self.farmer)["order_count"], 0)

    def test_direct_orders_are_never_settled(self):
        self.paid_order(route="direct")
        self.assertEqual(payout_service.pending_summary(self.farmer)["order_count"], 0)

    def test_orders_inside_the_hold_window_are_waiting(self):
        self.paid_order(age_days=2)
        self.assertEqual(payout_service.pending_summary(self.farmer)["order_count"], 0)

    def test_commission_and_delivery_are_tracked_separately(self):
        order = self.paid_order()
        summary = payout_service.pending_summary(self.farmer)
        self.assertEqual(summary["commission_amount"], order.platform_fee)
        self.assertEqual(summary["delivery_amount"], order.delivery_fee)
        # The farmer is owed the produce value only.
        self.assertEqual(summary["gross_amount"], order.subtotal)

    def test_hold_can_be_switched_off(self):
        self.paid_order(age_days=1)
        with override_settings(PAYOUT_HOLD_DAYS=0):
            self.assertEqual(payout_service.pending_summary(self.farmer)["order_count"], 1)


@override_settings(PAYOUT_HOLD_DAYS=7)
class BuildStatementTests(PayoutTestCase):
    def test_statement_snapshots_the_orders(self):
        first = self.paid_order("10")
        second = self.paid_order("5")
        start, end = self.period

        payout = payout_service.build_statement(self.farmer, period_start=start, period_end=end)

        self.assertEqual(payout.status, "draft")
        self.assertEqual(payout.order_count, 2)
        self.assertEqual(payout.net_amount, first.farmer_payout + second.farmer_payout)
        self.assertEqual(payout.items.count(), 2)

    def test_orders_leave_the_pending_queue(self):
        self.paid_order()
        start, end = self.period
        payout_service.build_statement(self.farmer, period_start=start, period_end=end)
        self.assertEqual(payout_service.pending_summary(self.farmer)["order_count"], 0)

    def test_a_second_run_finds_nothing_left_to_claim(self):
        self.paid_order()
        start, end = self.period
        payout_service.build_statement(self.farmer, period_start=start, period_end=end)

        # Eligibility excludes anything already in a payout, so re-running the
        # same period cannot create a second claim on the same money.
        with self.assertRaisesMessage(payout_service.PayoutError, "No settled orders"):
            payout_service.build_statement(
                self.farmer, period_start=start, period_end=end
            )
        self.assertEqual(PayoutItem.objects.count(), 1)

    def test_database_refuses_a_second_item_for_one_order(self):
        order = self.paid_order()
        start, end = self.period
        payout = payout_service.build_statement(self.farmer, period_start=start, period_end=end)
        from django.db import IntegrityError, transaction

        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                PayoutItem.objects.create(payout=payout, order=order, amount=Decimal("10"))

    def test_adjustments_change_the_net(self):
        self.paid_order()
        start, end = self.period
        payout = payout_service.build_statement(
            self.farmer, period_start=start, period_end=end, adjustments=Decimal("-50")
        )
        self.assertEqual(payout.net_amount, payout.gross_amount - Decimal("50.00"))

    def test_empty_period_is_refused(self):
        start, end = self.period
        with self.assertRaisesMessage(payout_service.PayoutError, "No settled orders"):
            payout_service.build_statement(self.farmer, period_start=start, period_end=end)

    def test_future_period_is_refused(self):
        today = timezone.localdate()
        with self.assertRaisesMessage(payout_service.PayoutError, "has not finished"):
            payout_service.build_statement(
                self.farmer, period_start=today, period_end=today + timedelta(days=3)
            )

    def test_build_all_skips_farmers_with_nothing_settled(self):
        self.paid_order()
        # A farmer who has listings but nothing settled should be reported as
        # skipped rather than producing an empty statement.
        quiet = make_farmer(username="quietfarmer")
        quiet_listing = make_listing(
            farmer=quiet, crop=make_crop("Mango"), quantity="10", price="50"
        )
        order_service.create_order(self.buyer, quiet_listing, Decimal("1"))
        start, end = self.period

        result = payout_service.build_all_statements(period_start=start, period_end=end)
        self.assertEqual(len(result["created"]), 1)
        self.assertEqual(result["created"][0].farmer, self.farmer)
        self.assertIn(str(quiet), result["skipped"])


@override_settings(PAYOUT_HOLD_DAYS=7)
class PayoutStateTests(PayoutTestCase):
    def setUp(self):
        super().setUp()
        self.paid_order()
        start, end = self.period
        self.payout = payout_service.build_statement(
            self.farmer, period_start=start, period_end=end
        )

    def test_happy_path(self):
        payout_service.approve(self.payout)
        self.payout.refresh_from_db()
        self.assertEqual(self.payout.status, "approved")
        self.assertIsNotNone(self.payout.approved_at)

        payout_service.mark_paid(self.payout, "UTR/NEFT 12345")
        self.payout.refresh_from_db()
        self.assertEqual(self.payout.status, "paid")
        self.assertEqual(self.payout.reference, "UTR/NEFT 12345")
        self.assertIsNotNone(self.payout.paid_at)
        self.assertTrue(self.payout.is_settled)
        self.assertIsNotNone(self.payout.items.first().settled_at)

    def test_cannot_pay_an_unapproved_statement(self):
        with self.assertRaisesMessage(payout_service.PayoutError, "Only approved"):
            payout_service.mark_paid(self.payout, "REF-1")

    def test_cannot_approve_twice(self):
        payout_service.approve(self.payout)
        with self.assertRaisesMessage(payout_service.PayoutError, "Only draft"):
            payout_service.approve(self.payout)

    def test_payment_reference_is_required(self):
        payout_service.approve(self.payout)
        with self.assertRaisesMessage(payout_service.PayoutError, "reference"):
            payout_service.mark_paid(self.payout, "   ")

    def test_cancel_releases_the_orders(self):
        payout_service.cancel(self.payout, reason="duplicate run")
        self.payout.refresh_from_db()
        self.assertEqual(self.payout.status, "cancelled")
        self.assertEqual(self.payout.items.count(), 0)
        self.assertEqual(payout_service.pending_summary(self.farmer)["order_count"], 1)

    def test_cannot_cancel_a_paid_statement(self):
        payout_service.approve(self.payout)
        payout_service.mark_paid(self.payout, "REF-2")
        with self.assertRaisesMessage(payout_service.PayoutError, "cannot be cancelled"):
            payout_service.cancel(self.payout)

    def test_negative_statements_cannot_be_approved(self):
        Payout.objects.filter(pk=self.payout.pk).update(net_amount=Decimal("-10.00"))
        self.payout.refresh_from_db()
        with self.assertRaisesMessage(payout_service.PayoutError, "negative payout"):
            payout_service.approve(self.payout)

    def test_totals_split_paid_and_outstanding(self):
        self.assertEqual(payout_service.farmer_totals(self.farmer)["outstanding_total"],
                         self.payout.net_amount)
        payout_service.approve(self.payout)
        payout_service.mark_paid(self.payout, "REF-3")
        totals = payout_service.farmer_totals(self.farmer)
        self.assertEqual(totals["paid_total"], self.payout.net_amount)
        self.assertEqual(totals["paid_orders"], 1)
        self.assertEqual(totals["outstanding_total"], Decimal("0.00"))


@override_settings(PAYOUT_HOLD_DAYS=7)
class PayoutCommandTests(PayoutTestCase):
    def test_command_drafts_statements(self):
        self.paid_order()
        out = __import__("io").StringIO()
        call_command(
            "run_payouts",
            "--days=60",
            stdout=out,
        )
        self.assertIn("statement(s) drafted", out.getvalue())
        self.assertEqual(Payout.objects.count(), 1)

    def test_dry_run_writes_nothing(self):
        self.paid_order()
        out = __import__("io").StringIO()
        call_command("run_payouts", "--days=60", "--dry-run", stdout=out)
        self.assertIn("no writes made", out.getvalue())
        self.assertEqual(Payout.objects.count(), 0)

    def test_bad_date_is_reported(self):
        from django.core.management.base import CommandError

        with self.assertRaises(CommandError):
            call_command("run_payouts", "--week-ending=not-a-date", stdout=__import__("io").StringIO())


@override_settings(PAYOUT_HOLD_DAYS=7)
class EarningsPageTests(PayoutTestCase):
    def test_farmer_sees_their_earnings(self):
        order = self.paid_order()
        self.client.force_login(self.farmer.user)
        response = self.client.get(reverse("marketplace:earnings"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Earnings")
        self.assertContains(response, f"{order.farmer_payout:.2f}")

    def test_a_buyer_cannot_open_it(self):
        self.client.force_login(self.buyer.user)
        response = self.client.get(reverse("marketplace:earnings"))
        self.assertEqual(response.status_code, 403)

    def test_staff_can_approve_and_pay(self):
        self.paid_order()
        start, end = self.period
        payout = payout_service.build_statement(
            self.farmer, period_start=start, period_end=end
        )
        staff = make_farmer(username="payoutstaff")
        staff.user.is_staff = True
        staff.user.save()
        self.client.force_login(staff.user)

        self.client.post(reverse("marketplace:payout_action", args=[payout.pk, "approve"]))
        payout.refresh_from_db()
        self.assertEqual(payout.status, "approved")

        self.client.post(
            reverse("marketplace:payout_action", args=[payout.pk, "pay"]),
            {"reference": "NEFT-99887"},
        )
        payout.refresh_from_db()
        self.assertEqual(payout.status, "paid")
        self.assertEqual(payout.reference, "NEFT-99887")

    def test_a_normal_user_cannot_approve(self):
        self.paid_order()
        start, end = self.period
        payout = payout_service.build_statement(
            self.farmer, period_start=start, period_end=end
        )
        self.client.force_login(self.farmer.user)
        self.client.post(reverse("marketplace:payout_action", args=[payout.pk, "approve"]))
        payout.refresh_from_db()
        self.assertEqual(payout.status, "draft")

# ===========================================================================
# Delivery partners — the other half of the money loop
# ===========================================================================
class PartnerPayoutTestCase(TestCase):
    """A transporter with completed shipments, dated far enough back to settle."""

    def setUp(self):
        self.partner = DeliveryPartner.objects.create(
            name="Ravi Transport",
            phone_number="9876500011",
            vehicle_type="tempo",
        )
        self.farmer = make_farmer(username="pfarmer")
        self.buyer = make_buyer(username="pbuyer")
        self.listing = make_listing(
            farmer=self.farmer, crop=make_crop("Onion"), quantity="500", price="30"
        )

    def completed_shipment(self, age_days=30, cost="250.00"):
        order = order_service.create_order(self.buyer, self.listing, Decimal("10"))
        shipment = logistics.plan_shipment([order.pk], partner=self.partner)
        shipment.status = "completed"
        shipment.planned_cost = Decimal(cost)
        shipment.save(update_fields=["status", "planned_cost"])
        if age_days:
            Shipment.objects.filter(pk=shipment.pk).update(
                created_at=timezone.now() - timedelta(days=age_days)
            )
            shipment.refresh_from_db()
        return shipment

    @property
    def period(self):
        today = timezone.localdate()
        return today - timedelta(days=40), today


@override_settings(PAYOUT_HOLD_DAYS=7)
class PartnerEligibilityTests(PartnerPayoutTestCase):
    def test_only_completed_shipments_are_eligible(self):
        shipment = self.completed_shipment()
        summary = payout_service.pending_partner_summary(self.partner)
        self.assertEqual(summary["shipment_count"], 1)
        self.assertEqual(summary["gross_amount"], shipment.planned_cost)

    def test_unfinished_shipments_are_not_payable_yet(self):
        order = order_service.create_order(self.buyer, self.listing, Decimal("10"))
        logistics.plan_shipment([order.pk], partner=self.partner)
        self.assertEqual(
            payout_service.pending_partner_summary(self.partner)["shipment_count"], 0
        )

    def test_shipments_inside_the_hold_window_are_waiting(self):
        self.completed_shipment(age_days=2)
        self.assertEqual(
            payout_service.pending_partner_summary(self.partner)["shipment_count"], 0
        )

    def test_a_cancelled_shipment_is_never_paid(self):
        shipment = self.completed_shipment()
        Shipment.objects.filter(pk=shipment.pk).update(status="cancelled")
        self.assertEqual(
            payout_service.pending_partner_summary(self.partner)["shipment_count"], 0
        )


@override_settings(PAYOUT_HOLD_DAYS=7)
class PartnerStatementTests(PartnerPayoutTestCase):
    def test_statement_snapshots_the_shipments(self):
        first = self.completed_shipment(cost="250.00")
        second = self.completed_shipment(cost="310.00")
        start, end = self.period

        payout = payout_service.build_partner_statement(
            self.partner, period_start=start, period_end=end
        )

        self.assertTrue(payout.is_partner_payout)
        self.assertIsNone(payout.farmer_id)
        self.assertEqual(payout.order_count, 2)
        self.assertEqual(payout.net_amount, first.planned_cost + second.planned_cost)
        self.assertEqual(payout.items.count(), 2)
        # Every rupee is delivery money, so gross and delivery agree.
        self.assertEqual(payout.gross_amount, payout.delivery_amount)
        self.assertEqual(payout.commission_amount, Decimal("0.00"))

    def test_a_payout_needs_exactly_one_recipient(self):
        from django.db import IntegrityError, transaction

        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                Payout.objects.create(
                    delivery_partner=self.partner,
                    farmer=self.farmer,
                    period_start=self.period[0],
                    period_end=self.period[1],
                    net_amount=Decimal("10.00"),
                )

    def test_payout_item_needs_exactly_one_source(self):
        from django.db import IntegrityError, transaction

        order = order_service.create_order(self.buyer, self.listing, Decimal("10"))
        shipment = self.completed_shipment()
        payout = payout_service.build_partner_statement(
            self.partner, period_start=self.period[0], period_end=self.period[1]
        )
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                PayoutItem.objects.create(
                    payout=payout,
                    order=order,
                    shipment=shipment,
                    amount=Decimal("10.00"),
                )

    def test_a_shipment_cannot_be_paid_twice(self):
        self.completed_shipment()
        start, end = self.period
        payout_service.build_partner_statement(
            self.partner, period_start=start, period_end=end
        )
        with self.assertRaisesMessage(payout_service.PayoutError, "No completed shipments"):
            payout_service.build_partner_statement(
                self.partner, period_start=start, period_end=end
            )

    def test_database_refuses_a_second_item_for_one_shipment(self):
        shipment = self.completed_shipment()
        payout = payout_service.build_partner_statement(
            self.partner, period_start=self.period[0], period_end=self.period[1]
        )
        from django.db import IntegrityError, transaction

        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                PayoutItem.objects.create(
                    payout=payout, shipment=shipment, amount=Decimal("10.00")
                )

    def test_adjustments_change_the_net(self):
        self.completed_shipment(cost="250.00")
        payout = payout_service.build_partner_statement(
            self.partner,
            period_start=self.period[0],
            period_end=self.period[1],
            adjustments=Decimal("-25"),
        )
        self.assertEqual(payout.net_amount, Decimal("225.00"))

    def test_full_state_machine_and_cancel_releases_the_shipment(self):
        self.completed_shipment()
        payout = payout_service.build_partner_statement(
            self.partner, period_start=self.period[0], period_end=self.period[1]
        )
        payout_service.approve(payout)
        payout.refresh_from_db()
        self.assertEqual(payout.status, "approved")

        payout_service.mark_paid(payout, "UTR/NEFT 55555")
        payout.refresh_from_db()
        self.assertEqual(payout.status, "paid")
        self.assertIsNotNone(payout.items.first().settled_at)
        self.assertEqual(
            payout_service.partner_totals(self.partner)["paid_total"], payout.net_amount
        )

    def test_cancel_returns_the_shipment_to_the_queue(self):
        self.completed_shipment()
        payout = payout_service.build_partner_statement(
            self.partner, period_start=self.period[0], period_end=self.period[1]
        )
        payout_service.cancel(payout, reason="wrong amount")
        self.assertEqual(payout.items.count(), 0)
        self.assertEqual(
            payout_service.pending_partner_summary(self.partner)["shipment_count"], 1
        )

    def test_build_all_partners_skips_those_with_nothing_completed(self):
        self.completed_shipment()
        DeliveryPartner.objects.create(name="Idle Hauler", phone_number="9876500022")
        result = payout_service.build_all_partner_statements(
            period_start=self.period[0], period_end=self.period[1]
        )
        self.assertEqual(len(result["created"]), 1)
        self.assertEqual(result["created"][0].delivery_partner, self.partner)
        self.assertTrue(any("Idle Hauler" in name for name in result["skipped"]))


@override_settings(PAYOUT_HOLD_DAYS=7)
class PartnerPayoutMethodTests(PartnerPayoutTestCase):
    def test_upi_destination_saves(self):
        method = deals_service.save_partner_payout_method(
            self.partner, kind="upi", upi_id="ravi@okaxis"
        )
        self.assertIsNone(method.user_id)
        self.assertEqual(method.delivery_partner, self.partner)
        self.assertEqual(
            deals_service.partner_payout_method_for(self.partner).upi_id, "ravi@okaxis"
        )

    def test_bad_upi_is_refused(self):
        with self.assertRaises(deals_service.DealError):
            deals_service.save_partner_payout_method(
                self.partner, kind="upi", upi_id="not-a-vpa"
            )

    def test_bank_destination_validates(self):
        with self.assertRaises(deals_service.DealError):
            deals_service.save_partner_payout_method(
                self.partner,
                kind="bank",
                account_holder="Ravi",
                account_number="12345",
                ifsc="HDFC0001234",
            )
        method = deals_service.save_partner_payout_method(
            self.partner,
            kind="bank",
            account_holder="Ravi Kumar",
            account_number="50100123456789",
            ifsc="HDFC0001234",
        )
        self.assertEqual(method.kind, "bank")

    def test_a_partner_method_is_not_a_farmer_method(self):
        deals_service.save_partner_payout_method(
            self.partner, kind="upi", upi_id="ravi@okaxis"
        )
        # The farmer's page must not offer the transporter's account.
        self.assertIsNone(deals_service.payout_method_for(self.farmer))


@override_settings(PAYOUT_HOLD_DAYS=7)
class PartnerCommandTests(PartnerPayoutTestCase):
    def test_command_settles_both_sides(self):
        self.completed_shipment()
        order = order_service.create_order(self.buyer, self.listing, Decimal("5"))
        order_service.mark_order_paid(order)
        Order.objects.filter(pk=order.pk).update(
            created_at=timezone.now() - timedelta(days=30)
        )

        out = __import__("io").StringIO()
        call_command("run_payouts", "--days=60", stdout=out)
        self.assertEqual(Payout.objects.count(), 2)
        self.assertIn("partner", out.getvalue())
        self.assertIn("farmer", out.getvalue())

    def test_dry_run_counts_both_sides_without_writing(self):
        self.completed_shipment()
        out = __import__("io").StringIO()
        call_command("run_payouts", "--days=60", "--dry-run", stdout=out)
        self.assertIn("no writes made", out.getvalue())
        self.assertIn("delivery charges", out.getvalue())
        self.assertEqual(Payout.objects.count(), 0)

    def test_only_farmers_leaves_partners_alone(self):
        self.completed_shipment()
        call_command("run_payouts", "--days=60", "--only=farmers", stdout=__import__("io").StringIO())
        self.assertEqual(Payout.objects.count(), 0)


@override_settings(PAYOUT_HOLD_DAYS=7)
class PartnerEarningsPageTests(PartnerPayoutTestCase):
    def test_partner_sees_their_earnings(self):
        shipment = self.completed_shipment(cost="412.50")
        user = self.partner_user()
        self.client.force_login(user)

        response = self.client.get(reverse("marketplace:partner_earnings"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Delivery earnings")
        self.assertContains(response, "412.50")

    def test_a_farmer_cannot_open_the_partner_page(self):
        self.client.force_login(self.farmer.user)
        self.assertEqual(
            self.client.get(reverse("marketplace:partner_earnings")).status_code, 403
        )

    def test_anonymous_is_redirected_to_login(self):
        response = self.client.get(reverse("marketplace:partner_earnings"))
        self.assertEqual(response.status_code, 302)
        self.assertIn("login", response["Location"])

    def test_partner_saves_payout_details(self):
        self.client.force_login(self.partner_user())
        response = self.client.post(
            reverse("marketplace:partner_payout_methods"),
            {"kind": "upi", "upi_id": "ravi@okaxis"},
        )
        self.assertEqual(response.status_code, 302)
        method = deals_service.partner_payout_method_for(self.partner)
        self.assertEqual(method.upi_id, "ravi@okaxis")

    def test_staff_can_approve_and_pay_a_partner_payout(self):
        self.completed_shipment()
        payout = payout_service.build_partner_statement(
            self.partner, period_start=self.period[0], period_end=self.period[1]
        )
        staff = make_farmer(username="partnerstaff")
        staff.user.is_staff = True
        staff.user.save()
        self.client.force_login(staff.user)

        self.client.post(reverse("marketplace:payout_action", args=[payout.pk, "approve"]))
        payout.refresh_from_db()
        self.assertEqual(payout.status, "approved")

        self.client.post(
            reverse("marketplace:payout_action", args=[payout.pk, "pay"]),
            {"reference": "NEFT-777"},
        )
        payout.refresh_from_db()
        self.assertEqual(payout.status, "paid")

    def partner_user(self):
        from django.contrib.auth import get_user_model

        user = get_user_model().objects.create_user(
            username="rivi", password="testpass123", email="ravi@example.com"
        )
        self.partner.user = user
        self.partner.save()
        return user

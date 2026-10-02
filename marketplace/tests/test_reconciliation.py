"""Tests for bank-statement reconciliation of direct-UPI claims.

The point of these is that a statement can only ever settle what it genuinely
matched: wrong amounts, duplicates and unknown references must stay unsettled.
"""

import csv
import io
from decimal import Decimal

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse

from marketplace.models import Order, Payment, Reconciliation
from marketplace.services import reconciliation as recon
from marketplace.services import upi
from marketplace.tests.factories import make_buyer, make_crop, make_farmer, make_listing, make_staff


def make_csv(header, rows, delimiter=","):
    buffer = io.StringIO()
    writer = csv.writer(buffer, delimiter=delimiter)
    writer.writerow(header)
    writer.writerows(rows)
    return buffer.getvalue()


def upload(name="statement.csv", text=""):
    return SimpleUploadedFile(name, text.encode("utf-8"), content_type="text/csv")


class ReconcileTestCase(TestCase):
    def setUp(self):
        self.farmer = make_farmer()
        self.buyer = make_buyer()
        crop = make_crop("Tomato")

        self.order_a = self._order(crop, "5", "40")
        self.order_b = self._order(crop, "3", "40")

        self.claim_a = upi.record_claim(self.order_a, "412233445566")
        self.claim_b = upi.record_claim(self.order_b, "998877665544")

    def _order(self, crop, quantity, price):
        listing = make_listing(farmer=self.farmer, crop=crop, quantity="100", price=price)
        return Order.objects.create(
            buyer=self.buyer,
            listing=listing,
            quantity_ordered=Decimal(quantity),
            agreed_price_per_unit=Decimal(price),
        )

    def amount(self, order):
        return order.total_amount


class ParsingTests(ReconcileTestCase):
    def test_common_bank_headers_are_recognised(self):
        text = make_csv(
            ["Date", "Description", "Utr Number", "Credit Amount"],
            [["01/10/2026", "UPI credit", "412233445566", f"{self.amount(self.order_a)}"]],
        )
        rows = recon.parse_statement(text)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].reference, "412233445566")
        self.assertEqual(rows[0].amount, self.amount(self.order_a))
        self.assertEqual(rows[0].value_date.isoformat(), "2026-10-01")

    def test_rn_utr_and_txn_id_variants(self):
        for header in (["RRN", "Amount"], ["Txn Id", "Credit"], ["Ref No", "Value"]):
            with self.subTest(header=header):
                rows = recon.parse_statement(
                    make_csv(header, [["412233445566", "1.00"]])
                )
                self.assertEqual(rows[0].reference, "412233445566")

    def test_currency_symbol_and_separators_are_stripped(self):
        rows = recon.parse_statement(
            make_csv(
                ["Transaction Reference", "Amount"],
                [["412233445566", "₹1,23,456.78"]],
            )
        )
        self.assertEqual(rows[0].amount, Decimal("123456.78"))

    def test_semicolon_delimited_and_bom(self):
        text = make_csv(["UTR", "Amount"], [["412233445566", "10.00"]], delimiter=";")
        rows = recon.parse_statement("﻿" + text)
        self.assertEqual(rows[0].reference, "412233445566")
        self.assertEqual(rows[0].amount, Decimal("10.00"))

    def test_trailing_whitespace_and_dot_zero_are_normalised(self):
        self.assertEqual(recon.normalise_reference(" 412233445566 "), "412233445566")
        self.assertEqual(recon.normalise_reference("412233445566.0"), "412233445566")

    def test_statement_without_reference_column_is_rejected(self):
        with self.assertRaisesMessage(recon.ReconciliationError, "reference column"):
            recon.parse_statement(make_csv(["Date", "Narration", "Amount"], [["x", "y", "1"]]))

    def test_empty_file_is_rejected(self):
        with self.assertRaisesMessage(recon.ReconciliationError, "empty"):
            recon.parse_statement("   ")

    def test_rows_without_a_reference_are_skipped(self):
        rows = recon.parse_statement(
            make_csv(["UTR", "Amount"], [["", "10"], ["412233445566", "10"]])
        )
        self.assertEqual(len(rows), 1)


class MatchingTests(ReconcileTestCase):
    def test_exact_match_is_reported(self):
        rows = recon.parse_statement(
            make_csv(
                ["UTR", "Credit Amount"],
                [["412233445566", str(self.amount(self.order_a))]],
            )
        )
        report = recon.reconcile(rows)
        self.assertEqual(len(report["matched"]), 1)
        self.assertEqual(report["matched"][0]["payment_id"], self.claim_a.pk)
        self.assertEqual(report["unmatched"], [])

    def test_amount_mismatch_is_not_matched(self):
        rows = recon.parse_statement(
            make_csv(["UTR", "Credit Amount"], [["412233445566", "1.00"]])
        )
        report = recon.reconcile(rows)
        self.assertEqual(report["matched"], [])
        self.assertEqual(report["unmatched"][0]["reason"], "amount_mismatch")
        self.assertEqual(report["unmatched"][0]["reference"], "412233445566")

    def test_paise_difference_within_tolerance_matches(self):
        order = self.order_a
        rows = recon.parse_statement(
            make_csv(
                ["UTR", "Amount"],
                [["412233445566", str(order.total_amount - Decimal("0.01"))]],
            )
        )
        self.assertEqual(len(recon.reconcile(rows)["matched"]), 1)

    def test_unknown_reference_is_reported(self):
        rows = recon.parse_statement(make_csv(["UTR", "Amount"], [["111111111111", "50"]]))
        report = recon.reconcile(rows)
        self.assertEqual(report["unmatched"][0]["reason"], "no_matching_claim")

    def test_already_settled_reference_is_reported(self):
        upi.confirm_claim(self.claim_a)
        rows = recon.parse_statement(
            make_csv(["UTR", "Amount"], [["412233445566", str(self.amount(self.order_a))]])
        )
        self.assertEqual(recon.reconcile(rows)["unmatched"][0]["reason"], "already_settled")

    def test_duplicate_line_in_file_is_reported_once(self):
        text = make_csv(
            ["UTR", "Amount"],
            [
                ["412233445566", str(self.amount(self.order_a))],
                ["412233445566", str(self.amount(self.order_a))],
            ],
        )
        report = recon.reconcile(recon.parse_statement(text))
        self.assertEqual(len(report["matched"]), 1)
        self.assertEqual(report["unmatched"][0]["reason"], "duplicate_in_file")

    def test_debits_are_ignored(self):
        rows = recon.parse_statement(
            make_csv(["UTR", "Amount"], [["412233445566", "-50.00"]])
        )
        self.assertEqual(recon.reconcile(rows)["unmatched"][0]["reason"], "debit")

    def test_statement_without_amount_column_matches_on_reference(self):
        rows = recon.parse_statement(
            make_csv(["Utr No", "Narration"], [["412233445566", "UPI credit"]])
        )
        report = recon.reconcile(rows)
        self.assertEqual(len(report["matched"]), 1)
        self.assertIn("no amount column", report["matched"][0]["warning"])

    def test_reconcile_does_not_settle_anything(self):
        rows = recon.parse_statement(
            make_csv(
                ["UTR", "Amount"],
                [
                    ["412233445566", str(self.amount(self.order_a))],
                    ["998877665544", str(self.amount(self.order_b))],
                ],
            )
        )
        report = recon.reconcile(rows)
        self.assertEqual(len(report["matched"]), 2)
        self.assertEqual(
            Payment.objects.filter(status="authorized", provider="upi_direct").count(), 2
        )
        self.order_a.refresh_from_db()
        self.assertEqual(self.order_a.payment_status, "pending")


@override_settings(UPI_ID="agrimarket@okaxis")
class ApplyTests(ReconcileTestCase):
    def upload_and_preview(self, rows, header=("Utr Number", "Credit Amount", "Date")):
        text = make_csv(list(header), rows)
        return recon.preview_statement(upload(text=text), "statement.csv")

    def test_apply_settles_matched_claims(self):
        record = self.upload_and_preview(
            [
                ["412233445566", str(self.amount(self.order_a)), "01/10/2026"],
                ["998877665544", str(self.amount(self.order_b)), "01/10/2026"],
            ]
        )
        self.assertFalse(record.applied)
        self.order_a.refresh_from_db()
        self.assertEqual(self.order_a.payment_status, "pending")

        result = recon.apply_reconciliation(record)
        self.assertEqual(result["confirmed"], 2)
        self.assertEqual(result["skipped"], [])

        for order in (self.order_a, self.order_b):
            order.refresh_from_db()
            self.assertEqual(order.payment_status, "paid")
            self.assertEqual(order.status, "paid")

        record.refresh_from_db()
        self.assertTrue(record.applied)
        self.assertIsNotNone(record.applied_at)

        self.claim_a.refresh_from_db()
        self.assertEqual(self.claim_a.status, "captured")
        self.assertIsNotNone(self.claim_a.reconciled_at)
        self.assertEqual(self.claim_a.reconciliation_id, record.pk)

    def test_amount_mismatch_is_left_pending(self):
        record = self.upload_and_preview(
            [
                ["412233445566", str(self.amount(self.order_a)), "01/10/2026"],
                ["998877665544", "1.00", "01/10/2026"],
            ]
        )
        result = recon.apply_reconciliation(record)
        self.assertEqual(result["confirmed"], 1)
        self.order_b.refresh_from_db()
        self.assertEqual(self.order_b.payment_status, "pending")
        self.claim_b.refresh_from_db()
        self.assertEqual(self.claim_b.status, "authorized")

    def test_applying_twice_is_refused(self):
        record = self.upload_and_preview(
            [["412233445566", str(self.amount(self.order_a)), "01/10/2026"]]
        )
        recon.apply_reconciliation(record)
        with self.assertRaisesMessage(recon.ReconciliationError, "already been applied"):
            recon.apply_reconciliation(record)

    def test_stale_match_is_skipped_not_crashed(self):
        record = self.upload_and_preview(
            [["412233445566", str(self.amount(self.order_a)), "01/10/2026"]]
        )
        # Somebody confirms it by hand between preview and apply.
        upi.confirm_claim(self.claim_a)
        result = recon.apply_reconciliation(record)
        self.assertEqual(result["confirmed"], 0)
        self.assertEqual(result["skipped"], ["412233445566"])

    def test_unmatched_summary_counts_reasons(self):
        record = self.upload_and_preview(
            [["111111111111", "5", "01/10/2026"], ["222222222222", "5", "01/10/2026"]]
        )
        self.assertEqual(
            recon.unmatched_summary(record), {"no_matching_claim": 2}
        )


@override_settings(UPI_ID="agrimarket@okaxis")
class ReconcileViewTests(TestCase):
    def setUp(self):
        self.farmer = make_farmer()
        self.buyer = make_buyer()
        listing = make_listing(
            farmer=self.farmer, crop=make_crop("Onion"), quantity="50", price="30"
        )
        self.order = Order.objects.create(
            buyer=self.buyer,
            listing=listing,
            quantity_ordered=Decimal("2"),
            agreed_price_per_unit=Decimal("30"),
        )
        self.claim = upi.record_claim(self.order, "412233445566")
        self.staff = make_staff()

    def statement(self, amount=None):
        return upload(
            text=make_csv(
                ["Date", "Utr Number", "Credit Amount"],
                [["01/10/2026", "412233445566", str(amount or self.order.total_amount)]],
            )
        )

    def test_non_staff_cannot_reach_the_page(self):
        self.client.force_login(self.buyer.user)
        response = self.client.get(reverse("marketplace:payment_reconcile"))
        self.assertEqual(response.status_code, 403)

    def test_anonymous_is_redirected_to_login(self):
        response = self.client.get(reverse("marketplace:payment_reconcile"))
        self.assertEqual(response.status_code, 302)
        self.assertIn("/login", response.url)

    def test_upload_creates_a_preview_and_settles_nothing(self):
        self.client.force_login(self.staff)
        response = self.client.post(
            reverse("marketplace:payment_reconcile"), {"statement": self.statement()}
        )
        record = Reconciliation.objects.get()
        self.assertRedirects(
            response, reverse("marketplace:payment_reconcile_preview", args=[record.pk])
        )
        self.assertFalse(record.applied)
        self.order.refresh_from_db()
        self.assertEqual(self.order.payment_status, "pending")

        preview = self.client.get(response.url)
        self.assertContains(preview, "412233445566")
        self.assertContains(preview, "Confirm 1 matched payment")

    def test_apply_from_the_preview_settles_the_order(self):
        self.client.force_login(self.staff)
        self.client.post(reverse("marketplace:payment_reconcile"), {"statement": self.statement()})
        record = Reconciliation.objects.get()

        response = self.client.post(
            reverse("marketplace:payment_reconcile"), {"reconciliation": record.pk}
        )
        self.assertEqual(response.status_code, 302)
        self.order.refresh_from_db()
        self.assertEqual(self.order.payment_status, "paid")
        record.refresh_from_db()
        self.assertTrue(record.applied)

    def test_upload_without_a_file_is_reported(self):
        self.client.force_login(self.staff)
        response = self.client.post(reverse("marketplace:payment_reconcile"), {})
        self.assertRedirects(response, reverse("marketplace:payment_reconcile"))
        self.assertFalse(Reconciliation.objects.exists())

    def test_unreadable_statement_is_reported_not_crashed(self):
        self.client.force_login(self.staff)
        self.client.post(
            reverse("marketplace:payment_reconcile"),
            {"statement": upload(text=make_csv(["Date", "Amount"], [["01/10/2026", "10"]]))},
        )
        self.assertFalse(Reconciliation.objects.exists())

    def test_pending_badge_is_shown_to_staff(self):
        self.client.force_login(self.staff)
        response = self.client.get(reverse("marketplace:payment_reconcile"))
        self.assertContains(response, "1 payment awaiting confirmation")
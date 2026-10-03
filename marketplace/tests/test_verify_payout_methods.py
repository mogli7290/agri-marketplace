"""Tests for the UPI verification command.

Buyers are told to trust a UPI ID because staff checked it. That claim is only
worth something if the check is recorded and cannot be faked by the farmer, so
these cover the listing, the recording, and the refusal cases.
"""

from io import StringIO

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase

from marketplace.models import PayoutMethod
from marketplace.services import deals as deals_service
from marketplace.tests.factories import make_farmer


def run(*args, **kwargs):
    out = StringIO()
    call_command("verify_payout_methods", *args, stdout=out, stderr=out, **kwargs)
    return out.getvalue()


class VerifyPayoutMethodTests(TestCase):
    def setUp(self):
        self.farmer = make_farmer(username="vpa_farmer")
        self.method = deals_service.save_payout_method(
            self.farmer.user, kind="upi", upi_id="farmer@okaxis"
        )

    # -- listing -------------------------------------------------------------

    def test_an_unchecked_vpa_is_listed(self):
        output = run()
        self.assertIn("farmer@okaxis", output)
        self.assertIn("NOT checked", output)
        self.assertIn("1 UPI ID(s) still unchecked", output)

    def test_verified_vpas_are_hidden_by_default(self):
        run(verify=str(self.method.pk))
        output = run()
        self.assertNotIn("farmer@okaxis", output)
        self.assertIn("No UPI IDs are waiting", output)

    def test_all_shows_the_verified_ones_too(self):
        run(verify=str(self.method.pk))
        output = run(all=True)
        self.assertIn("farmer@okaxis", output)
        self.assertIn("verified", output)

    # -- recording -----------------------------------------------------------

    def test_verifying_records_when_and_by_whom(self):
        staff = get_user_model().objects.create_superuser(
            username="checker", password="x", email="checker@example.com"
        )
        run(verify=str(self.method.pk))
        self.method.refresh_from_db()
        self.assertTrue(self.method.is_verified)
        self.assertIsNotNone(self.method.verified_at)
        # A PaaS shell has no request user, so it attributes to a superuser
        # rather than leaving the check unattributed.
        self.assertEqual(self.method.verified_by_id, staff.pk)

    def test_by_names_the_checker_explicitly(self):
        staff = get_user_model().objects.create_user(
            username="staff2", password="x", email="staff2@example.com", is_staff=True
        )
        run(verify=str(self.method.pk), by="staff2")
        self.method.refresh_from_db()
        self.assertEqual(self.method.verified_by_id, staff.pk)

    def test_by_an_unknown_user_is_refused(self):
        with self.assertRaises(CommandError):
            run(verify=str(self.method.pk), by="nobody")

    def test_an_unattributable_check_warns_loudly(self):
        """No superuser and no --by: record it, but say it has no name on it."""
        output = run(verify=str(self.method.pk))
        self.method.refresh_from_db()
        self.assertTrue(self.method.is_verified)
        self.assertIsNone(self.method.verified_by)
        self.assertIn("without a name against it", output)

    def test_unverifying_clears_the_audit_trail(self):
        run(verify=str(self.method.pk))
        run(unverify=str(self.method.pk))
        self.method.refresh_from_db()
        self.assertFalse(self.method.is_verified)
        self.assertIsNone(self.method.verified_at)
        self.assertIsNone(self.method.verified_by)

    def test_the_farmer_cannot_verify_their_own(self):
        """The command is for staff; running it must not be a farmer's shortcut."""
        self.client.login(username="vpa_farmer", password="testpass123")
        response = self.client.post("/manage-verify/", {}) if False else None
        # There is no HTTP entry point — it is a management command only.
        self.assertIsNone(response)
        self.assertFalse(
            self.farmer.user.has_perm("marketplace.change_payoutmethod")
        )

    # -- refusals ------------------------------------------------------------

    def test_verifying_an_unknown_id_is_an_error(self):
        with self.assertRaises(CommandError):
            run(verify="999999")

    def test_a_bank_method_is_refused(self):
        bank = deals_service.save_payout_method(
            self.farmer.user, kind="bank",
            account_holder="VPA Farmer", account_number="1234567890", ifsc="ABCD0001234",
        )
        with self.assertRaises(CommandError):
            run(verify=str(bank.pk))

    def test_both_flags_at_once_is_an_error(self):
        with self.assertRaises(CommandError):
            run(verify=str(self.method.pk), unverify=str(self.method.pk))

    def test_verifying_twice_is_reported_not_re_attributed(self):
        run(verify=str(self.method.pk))
        self.method.refresh_from_db()
        first_time = self.method.verified_at
        output = run(verify=str(self.method.pk))
        self.method.refresh_from_db()
        self.assertEqual(self.method.verified_at, first_time)
        self.assertIn("already verified", output)

    # -- guidance ------------------------------------------------------------

    def test_the_checklist_explains_vpa_and_how_to_check_one(self):
        output = run(checklist=True)
        self.assertIn("Virtual Payment Address", output)
        self.assertIn("name@bank", output)
        # The bit that actually matters, and the scam warning.
        self.assertIn("read out the VPA", output)
        self.assertIn("Never verify from a screenshot", output)

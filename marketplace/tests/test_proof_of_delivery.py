"""Tests for proof of delivery.

The guarantees that matter:

* a driver cannot close a stop without the buyer's code, unless they are staff
  (and then the override is attributed),
* a wrong code cannot be brute-forced,
* a dispute holds the transporter's money until staff resolve it,
* neither side can edit the other's evidence.
"""

from datetime import timedelta
from decimal import Decimal
from io import BytesIO

from django.contrib.auth import get_user_model
from django.core import mail
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from marketplace.models import DeliveryPartner, DeliveryProof, Shipment, ShipmentStop
from marketplace.services import delivery as delivery_service
from marketplace.services import logistics
from marketplace.services import orders as order_service
from marketplace.services import payouts as payout_service
from marketplace.tests.factories import make_buyer, make_crop, make_farmer, make_listing, make_staff

User = get_user_model()

# A path with enough line commands to look like an actual drawing.
SIGNATURE = "M 20 60 L 60 30 L 100 70 L 140 25 L 180 65 L 220 40"


def make_image(name="proof.jpg", content_type="image/jpeg", size=1024):
    """A tiny valid PNG, regardless of the declared content type."""
    from PIL import Image

    buffer = BytesIO()
    Image.new("RGB", (8, 8), (0, 128, 0)).save(buffer, format="PNG")
    return SimpleUploadedFile(
        name, buffer.getvalue(), content_type=content_type
    )


@override_settings(SITE_URL="https://agri.example.com", NOTIFICATIONS_ENABLED=True)
class ProofTestCase(TestCase):
    def setUp(self):
        self.farmer = make_farmer(username="pod_farmer")
        self.buyer = make_buyer(username="pod_buyer")
        self.listing = make_listing(
            farmer=self.farmer, crop=make_crop("Tomato"), quantity="500", price="40"
        )
        self.partner = DeliveryPartner.objects.create(
            name="Ravi Transport", phone_number="9876500099"
        )
        self.driver = User.objects.create_user(
            username="pod_driver", password="testpass123", email="ravi@example.com"
        )
        self.partner.user = self.driver
        self.partner.save()

        order_service.advance_status(
            order_service.create_order(self.buyer, self.listing, Decimal("10")),
            "confirmed",
        )
        self.order = order_service.create_order(self.buyer, self.listing, Decimal("10"))
        order_service.mark_order_paid(self.order)
        self.shipment = logistics.plan_shipment([self.order.pk], partner=self.partner)
        self.stop = self.shipment.stops.get(kind="delivery")
        self.staff = make_staff(username="pod_staff")

    def issue(self):
        return delivery_service.issue_code(self.order)

    def recorded(self, *, as_driver=False):
        """A proof on file for this order, with a code when a driver made it."""
        if as_driver:
            return delivery_service.record_proof(
                self.order, user=self.driver, partner=self.partner,
                code=self.issue(), signature=SIGNATURE,
            )
        return delivery_service.record_proof(
            self.order, user=self.staff, partner=self.partner, signature=SIGNATURE
        )

    def send_now(self):
        """Run the on_commit callbacks a real request would fire."""
        return self.captureOnCommitCallbacks(execute=True)


class CodeTests(ProofTestCase):
    def test_issuing_a_code_emails_the_buyer_only(self):
        with self.send_now():
            code = self.issue()
        self.assertEqual(len(code), 6)
        self.assertTrue(code.isdigit())
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, [self.buyer.user.email])
        self.assertIn(code, mail.outbox[0].body)
        self.assertNotIn(self.driver.email, [m.to[0] for m in mail.outbox])

    def test_only_the_hash_is_stored(self):
        code = self.issue()
        proof = DeliveryProof.objects.get(order=self.order)
        self.assertEqual(len(proof.otp_hash), 64)
        self.assertNotIn(code, proof.otp_hash)
        self.assertNotEqual(proof.otp_hash, code)

    def test_a_new_code_supersedes_the_old_one(self):
        first = self.issue()
        second = self.issue()
        self.assertNotEqual(first, second)
        delivery_service.verify_code(self.order, second)
        with self.assertRaises(delivery_service.DeliveryError):
            delivery_service.verify_code(self.order, first)

    def test_the_wrong_code_is_refused(self):
        code = self.issue()
        wrong = "000000" if code != "000000" else "111111"
        with self.assertRaisesMessage(delivery_service.DeliveryError, "not right"):
            delivery_service.verify_code(self.order, wrong)

    def test_brute_force_is_capped(self):
        self.issue()
        for _ in range(delivery_service.MAX_CODE_ATTEMPTS):
            with self.assertRaises(delivery_service.DeliveryError):
                delivery_service.verify_code(self.order, "999999")
        with self.assertRaisesMessage(delivery_service.DeliveryError, "Too many wrong codes"):
            delivery_service.verify_code(self.order, "999999")

    def test_a_correct_code_still_works_after_wrong_ones(self):
        code = self.issue()
        with self.assertRaises(delivery_service.DeliveryError):
            delivery_service.verify_code(self.order, "000000")
        proof = delivery_service.verify_code(self.order, code)
        self.assertIsNotNone(proof.otp_confirmed_at)
        self.assertTrue(proof.is_confirmed)

    def test_an_expired_code_is_refused(self):
        code = self.issue()
        DeliveryProof.objects.filter(order=self.order).update(
            otp_sent_at=timezone.now() - timedelta(days=2)
        )
        with self.assertRaisesMessage(delivery_service.DeliveryError, "expired"):
            delivery_service.verify_code(self.order, code)

    def test_issuing_needs_a_delivery_route(self):
        loose = order_service.create_order(self.buyer, self.listing, Decimal("1"))
        with self.assertRaisesMessage(delivery_service.DeliveryError, "not on a delivery route"):
            delivery_service.issue_code(loose)


class RecordingTests(ProofTestCase):
    def test_a_driver_needs_the_code(self):
        self.issue()
        with self.assertRaisesMessage(delivery_service.DeliveryError, "Enter the buyer"):
            delivery_service.record_proof(
                self.order, user=self.driver, partner=self.partner,
                signature=SIGNATURE,
            )

    def test_the_code_closes_the_stop(self):
        code = self.issue()
        proof = delivery_service.record_proof(
            self.order, user=self.driver, partner=self.partner, code=code,
            signature=SIGNATURE, notes="Left with the gatekeeper",
        )
        self.assertTrue(proof.has_otp)
        self.assertTrue(proof.has_signature)
        self.assertEqual(proof.recorded_by, self.partner)
        self.stop.refresh_from_db()
        self.assertTrue(self.stop.is_closed)
        self.assertIsNotNone(self.stop.completed_at)

    def test_staff_can_record_without_a_code(self):
        proof = delivery_service.record_proof(
            self.order, user=self.staff, signature=SIGNATURE,
            notes="Buyer unreachable",
        )
        self.assertTrue(proof.has_signature)
        self.assertFalse(proof.has_otp)
        self.assertEqual(proof.recorded_by_user, self.staff)

    def test_a_photo_alone_is_enough_for_staff(self):
        proof = delivery_service.record_proof(
            self.order, user=self.staff, photo=make_image(),
        )
        self.assertTrue(proof.has_photo)

    def test_a_photo_alone_is_not_enough_for_a_driver(self):
        with self.assertRaisesMessage(delivery_service.DeliveryError, "Enter the buyer"):
            delivery_service.record_proof(
                self.order, user=self.driver, partner=self.partner, photo=make_image(),
            )

    def test_nothing_at_all_is_refused(self):
        with self.assertRaisesMessage(delivery_service.DeliveryError, "not nothing"):
            delivery_service.record_proof(self.order, user=self.staff)

    def test_an_oversized_photo_is_refused(self):
        big = SimpleUploadedFile("big.jpg", b"x" * (6 * 1024 * 1024), "image/jpeg")
        with self.assertRaisesMessage(delivery_service.DeliveryError, "too large"):
            delivery_service.record_proof(self.order, user=self.staff, photo=big)

    def test_a_wrong_file_type_is_refused(self):
        doc = SimpleUploadedFile("notes.pdf", b"%PDF-1.4", "application/pdf")
        with self.assertRaisesMessage(delivery_service.DeliveryError, "JPEG"):
            delivery_service.record_proof(self.order, user=self.staff, photo=doc)

    def test_a_placeholder_signature_is_refused(self):
        with self.assertRaisesMessage(delivery_service.DeliveryError, "does not look like"):
            delivery_service.record_proof(
                self.order, user=self.staff, signature="hello there"
            )

    def test_the_buyer_is_told_when_a_delivery_is_claimed(self):
        mail.outbox.clear()
        with self.send_now():
            delivery_service.record_proof(self.order, user=self.staff, signature=SIGNATURE)
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, [self.buyer.user.email])
        self.assertIn("was delivered", mail.outbox[0].subject)

    def test_evidence_count_reflects_what_was_captured(self):
        proof = delivery_service.record_proof(self.order, user=self.staff, signature=SIGNATURE)
        self.assertEqual(proof.evidence_count, 1)
        code = self.issue()
        proof = delivery_service.record_proof(
            self.order, user=self.driver, partner=self.partner, code=code
        )
        self.assertEqual(proof.evidence_count, 2)


class BuyerResponseTests(ProofTestCase):
    def test_the_buyer_can_confirm(self):
        self.recorded()
        proof = delivery_service.confirm_by_buyer(self.order, self.buyer.user)
        self.assertEqual(proof.status, "confirmed")
        self.assertEqual(proof.confirmed_by_buyer, self.buyer.user)

    def test_confirming_tells_the_transporter(self):
        self.recorded()
        mail.outbox.clear()

        with self.send_now():
            delivery_service.confirm_by_buyer(self.order, self.buyer.user)
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, [self.driver.email])
        self.assertIn("confirmed", mail.outbox[0].subject)

    def test_confirming_twice_is_harmless(self):
        self.recorded()
        delivery_service.confirm_by_buyer(self.order, self.buyer.user)
        again = delivery_service.confirm_by_buyer(self.order, self.buyer.user)
        self.assertEqual(again.status, "confirmed")

    def test_the_buyer_can_dispute(self):
        self.recorded()
        proof = delivery_service.confirm_by_buyer(
            self.order, self.buyer.user, accept=False, reason="Only 5 kg arrived",
        )
        self.assertTrue(proof.is_disputed)
        self.assertIn("5 kg", proof.dispute_reason)

    def test_a_dispute_needs_a_reason(self):
        self.recorded()
        with self.assertRaisesMessage(delivery_service.DeliveryError, "what was wrong"):
            delivery_service.confirm_by_buyer(self.order, self.buyer.user, accept=False)

    def test_confirming_something_never_delivered_is_refused(self):
        with self.assertRaisesMessage(delivery_service.DeliveryError, "No delivery"):
            delivery_service.confirm_by_buyer(self.order, self.buyer.user)


class DisputeResolutionTests(ProofTestCase):
    def disputed(self):
        self.recorded()
        return delivery_service.confirm_by_buyer(
            self.order, self.buyer.user, accept=False, reason="Produce was spoiled",
        )

    def test_the_transporter_is_told_they_are_disputed(self):
        self.recorded(as_driver=True)
        mail.outbox.clear()
        with self.send_now():
            delivery_service.confirm_by_buyer(
                self.order, self.buyer.user, accept=False, reason="Nothing arrived",
            )
        self.assertEqual(mail.outbox[0].to, [self.driver.email])
        self.assertIn("looking into", mail.outbox[0].subject)
        self.assertIn("Nothing arrived", mail.outbox[0].body)

    def test_staff_can_release_the_money(self):
        self.disputed()
        proof = delivery_service.resolve_dispute(
            self.order, self.staff, "release", notes="Gatekeeper signed"
        )
        self.assertEqual(proof.status, "confirmed")
        self.assertIn("release", proof.dispute_reason)

    def test_staff_can_refund_the_buyer(self):
        self.disputed()
        delivery_service.resolve_dispute(self.order, self.staff, "refund")
        proof = DeliveryProof.objects.get(order=self.order)
        self.assertEqual(proof.status, "confirmed")
        self.assertIn("refund", proof.dispute_reason)

    def test_an_unknown_resolution_is_refused(self):
        self.disputed()
        with self.assertRaisesMessage(delivery_service.DeliveryError, "refund the buyer"):
            delivery_service.resolve_dispute(self.order, self.staff, "shrug")

    def test_resolving_something_undisputed_is_refused(self):
        self.recorded()
        with self.assertRaisesMessage(delivery_service.DeliveryError, "not disputed"):
            delivery_service.resolve_dispute(self.order, self.staff, "release")


class ShipmentAndPayoutTests(ProofTestCase):
    def test_a_shipment_stays_open_until_every_stop_is_closed(self):
        self.recorded()
        # This shipment's only delivery stop is now proven, so it can close.
        delivery_service.close_shipment(self.shipment)
        self.shipment.refresh_from_db()
        self.assertEqual(self.shipment.status, "completed")

    def test_an_open_stop_blocks_completion(self):
        other = Shipment.objects.create(partner=self.partner)
        ShipmentStop.objects.create(
            shipment=other, kind="delivery", label="Delivery: someone", sequence=0
        )
        with self.assertRaisesMessage(delivery_service.DeliveryError, "still open"):
            delivery_service.close_shipment(other)

    def test_staff_can_force_completion(self):
        other = Shipment.objects.create(partner=self.partner)
        ShipmentStop.objects.create(
            shipment=other, kind="delivery", label="Delivery: someone", sequence=0
        )
        delivery_service.close_shipment(other, force=True, user=self.staff)
        other.refresh_from_db()
        self.assertEqual(other.status, "completed")
        self.assertTrue(other.stops.first().is_closed)

    def test_a_disputed_delivery_holds_the_transporters_money(self):
        self.recorded()
        self.shipment.status = "completed"
        self.shipment.save(update_fields=["status"])
        Shipment.objects.filter(pk=self.shipment.pk).update(
            created_at=timezone.now() - timedelta(days=30)
        )

        self.assertEqual(
            payout_service.pending_partner_summary(self.partner)["shipment_count"], 1
        )

        delivery_service.confirm_by_buyer(
            self.order, self.buyer.user, accept=False, reason="Never arrived"
        )
        self.assertEqual(
            payout_service.pending_partner_summary(self.partner)["shipment_count"], 0
        )

        # Staff release it and the money becomes payable again.
        delivery_service.resolve_dispute(self.order, self.staff, "release")
        self.assertEqual(
            payout_service.pending_partner_summary(self.partner)["shipment_count"], 1
        )


class WebTests(ProofTestCase):
    def test_the_buyer_sees_their_code_on_the_order_page(self):
        self.client.force_login(self.buyer.user)
        self.client.post(reverse("marketplace:delivery_code", args=[self.order.pk]))
        response = self.client.get(reverse("marketplace:order_detail", args=[self.order.pk]))
        self.assertContains(response, "Proof of delivery")
        self.assertContains(response, "Your delivery code")

    def test_the_driver_records_through_the_shipment_page(self):
        code = self.issue()
        self.client.force_login(self.driver)
        response = self.client.post(
            reverse("marketplace:delivery_proof", args=[self.order.pk]),
            {"code": code, "signature": SIGNATURE},
        )
        self.assertEqual(response.status_code, 302)
        proof = DeliveryProof.objects.get(order=self.order)
        self.assertTrue(proof.has_otp)
        self.assertTrue(proof.has_signature)

    def test_a_farmer_cannot_record_a_delivery(self):
        code = self.issue()
        self.client.force_login(self.farmer.user)
        self.client.post(
            reverse("marketplace:delivery_proof", args=[self.order.pk]), {"code": code}
        )
        # A code was issued for this order, but the farmer added no evidence to it.
        proof = DeliveryProof.objects.get(order=self.order)
        self.assertEqual(proof.evidence_count, 0)
        self.stop.refresh_from_db()
        self.assertFalse(self.stop.is_closed)

    def test_the_buyer_can_dispute_from_the_web(self):
        self.recorded()
        self.client.force_login(self.buyer.user)
        self.client.post(
            reverse("marketplace:delivery_response", args=[self.order.pk]),
            {"action": "dispute", "reason": "Wrong quantity"},
        )
        proof = DeliveryProof.objects.get(order=self.order)
        self.assertTrue(proof.is_disputed)

    def test_an_anonymous_visitor_is_sent_to_login(self):
        response = self.client.post(
            reverse("marketplace:delivery_code", args=[self.order.pk])
        )
        self.assertEqual(response.status_code, 302)
        self.assertIn("login", response["Location"])

    def test_the_shipment_page_offers_the_capture_form(self):
        self.client.force_login(self.driver)
        response = self.client.get(
            reverse("marketplace:shipment_detail", args=[self.shipment.pk])
        )
        self.assertContains(response, "Close a delivery stop")
        self.assertContains(response, "sigPad")

    def test_only_staff_can_force_complete(self):
        self.client.force_login(self.driver)
        response = self.client.post(
            reverse("marketplace:shipment_complete", args=[self.shipment.pk])
        )
        self.assertEqual(response.status_code, 403)
        self.shipment.refresh_from_db()
        self.assertEqual(self.shipment.status, "planned")

    def test_staff_can_resolve_a_dispute_from_the_web(self):
        self.recorded()
        delivery_service.confirm_by_buyer(
            self.order, self.buyer.user, accept=False, reason="Spoiled"
        )
        self.client.force_login(self.staff)
        self.client.post(
            reverse("marketplace:delivery_dispute_resolve", args=[self.order.pk, "release"])
        )
        proof = DeliveryProof.objects.get(order=self.order)
        self.assertEqual(proof.status, "confirmed")

class DriverCanRequestACodeTests(ProofTestCase):
    """The driver must not be stranded at the door with no code in existence."""

    def test_the_assigned_driver_can_issue_a_code_to_the_buyer(self):
        self.client.force_login(self.driver)
        with self.send_now():
            response = self.client.post(
                reverse("marketplace:delivery_code", args=[self.order.pk])
            )
        self.assertEqual(response.status_code, 302)
        proof = DeliveryProof.objects.get(order=self.order)
        self.assertTrue(proof.has_code_issued)
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, [self.buyer.user.email])

    def test_the_driver_is_not_shown_the_code_in_their_own_session(self):
        """Only the buyer reads the code back to the driver, not the other way round."""
        self.client.force_login(self.driver)
        with self.send_now():
            self.client.post(reverse("marketplace:delivery_code", args=[self.order.pk]))
        response = self.client.get(reverse("marketplace:order_detail", args=[self.order.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "Your delivery code:")

    def test_the_driver_can_open_the_order_they_delivered(self):
        """They recorded a proof against this page; a 404 there would be absurd."""
        self.client.force_login(self.driver)
        response = self.client.get(reverse("marketplace:order_detail", args=[self.order.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Proof of delivery")

    def test_an_unrelated_driver_cannot_open_someone_elses_order(self):
        rival = DeliveryPartner.objects.create(name="Rival Haulage 2", phone_number="9876510001")
        rival_user = User.objects.create_user(
            username="pod_rival2", password="testpass123", email="rival2@example.com"
        )
        rival.user = rival_user
        rival.save()

        self.client.force_login(rival_user)
        response = self.client.get(reverse("marketplace:order_detail", args=[self.order.pk]))
        self.assertEqual(response.status_code, 404)

    def test_an_unrelated_driver_cannot(self):
        rival = DeliveryPartner.objects.create(name="Rival Haulage", phone_number="9876510000")
        rival_user = User.objects.create_user(
            username="pod_rival", password="testpass123", email="rival@example.com"
        )
        rival.user = rival_user
        rival.save()

        self.client.force_login(rival_user)
        self.client.post(reverse("marketplace:delivery_code", args=[self.order.pk]))
        self.assertFalse(DeliveryProof.objects.filter(order=self.order).exists())

    def test_a_random_buyer_cannot(self):
        other = make_buyer(username="pod_stranger")
        self.client.force_login(other.user)
        self.client.post(reverse("marketplace:delivery_code", args=[self.order.pk]))
        self.assertFalse(DeliveryProof.objects.filter(order=self.order).exists())

    def test_the_shipment_page_flags_stops_with_no_code(self):
        self.client.force_login(self.driver)
        response = self.client.get(
            reverse("marketplace:shipment_detail", args=[self.shipment.pk])
        )
        self.assertContains(response, "no code issued yet")
        self.assertContains(response, "Email the buyer a delivery code")

        self.issue()
        response = self.client.get(
            reverse("marketplace:shipment_detail", args=[self.shipment.pk])
        )
        self.assertNotContains(response, "no code issued yet")

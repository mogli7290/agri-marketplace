"""Tests for the demand board: requests, offers, conversations and direct payments.

The rules worth protecting:

* the platform fee only exists on platform-collected orders,
* contact details stay hidden until a real order exists,
* only the buyer accepts, only the farmer withdraws,
* only the farmer who sold can confirm a direct payment.
"""

from decimal import Decimal

from django.test import TestCase, override_settings
from django.urls import reverse

from marketplace.models import (
    Conversation,
    DemandRequest,
    Message,
    Order,
    PayoutMethod,
    Payment,
    RequestOffer,
)
from marketplace.services import deals as deals_service
from marketplace.services import orders as order_service
from marketplace.services import upi
from marketplace.tests.factories import make_buyer, make_crop, make_farmer, make_listing


class DealsTestCase(TestCase):
    def setUp(self):
        self.farmer = make_farmer(username="boardfarmer")
        self.other_farmer = make_farmer(username="boardfarmer2", phone_number="9000022222")
        self.buyer = make_buyer(username="boardbuyer")
        self.crop = make_crop("Tomato")
        make_listing(farmer=self.farmer, crop=self.crop, quantity="500", price="40")
        self.request = deals_service.create_request(
            self.buyer,
            crop=self.crop,
            quantity=Decimal("100"),
            delivery_city="Pune",
            target_price=Decimal("35"),
        )


class RequestTests(DealsTestCase):
    def test_buyer_can_post_a_request(self):
        self.assertEqual(self.request.status, "open")
        self.assertEqual(self.request.buyer, self.buyer)
        self.assertEqual(self.request.quantity, Decimal("100.00"))

    def test_invalid_request_is_refused(self):
        with self.assertRaises(deals_service.DealError):
            deals_service.create_request(
                self.buyer, crop=self.crop, quantity=Decimal("0"), delivery_city="Pune"
            )
        with self.assertRaises(deals_service.DealError):
            deals_service.create_request(
                self.buyer, crop=self.crop, quantity=Decimal("5"), delivery_city="  "
            )

    def test_buyer_can_close_their_own_request(self):
        deals_service.close_request(self.request, user=self.buyer.user)
        self.request.refresh_from_db()
        self.assertEqual(self.request.status, "cancelled")

    def test_a_stranger_cannot_close_someone_elses_request(self):
        with self.assertRaises(deals_service.DealError):
            deals_service.close_request(self.request, user=self.other_farmer.user)

    def test_closed_request_cannot_be_offered_on(self):
        deals_service.close_request(self.request, user=self.buyer.user)
        with self.assertRaises(deals_service.DealError):
            deals_service.create_offer(
                self.request, self.farmer, price_per_unit=Decimal("38")
            )

    def test_farmer_sees_the_board(self):
        self.assertIn(self.request, deals_service.open_requests_for(self.farmer))
        self.assertEqual(
            deals_service.open_requests_for(self.farmer, crop=make_crop("Mango")).count(), 0
        )


class OfferTests(DealsTestCase):
    def offer(self, farmer=None, route="platform", price="38"):
        return deals_service.create_offer(
            self.request, farmer or self.farmer,
            price_per_unit=Decimal(price), payment_route=route,
        )

    def test_farmer_offer_opens_a_conversation(self):
        offer = self.offer()
        self.assertEqual(offer.status, "pending")
        conversation = Conversation.objects.get(request=self.request, farmer=self.farmer)
        self.assertEqual(conversation.buyer, self.buyer)
        self.assertFalse(conversation.deal_struck)

    def test_duplicate_pending_offer_is_refused(self):
        self.offer()
        with self.assertRaises(deals_service.DealError):
            self.offer()

    def test_direct_route_requires_a_upi_id(self):
        with self.assertRaises(deals_service.DealError):
            self.offer(route="direct")

    def test_direct_route_allowed_once_upi_is_saved(self):
        deals_service.save_payout_method(self.farmer.user, kind="upi", upi_id="farmer@okaxis")
        offer = self.offer(route="direct")
        self.assertEqual(offer.payment_route, "direct")

    def test_accepting_creates_an_order_with_no_platform_fee(self):
        offer = self.offer(route="platform", price="38")
        deals_service.respond_offer(offer, "accept", self.buyer.user)

        order = Order.objects.get(pk=offer.order_id)
        self.assertEqual(order.payment_route, "platform")
        self.assertGreater(order.platform_fee, Decimal("0"))
        self.assertEqual(order.total_amount, order.subtotal + order.platform_fee)

    def test_direct_offer_creates_a_zero_fee_order(self):
        deals_service.save_payout_method(self.farmer.user, kind="upi", upi_id="farmer@okaxis")
        offer = self.offer(route="direct")
        deals_service.respond_offer(offer, "accept", self.buyer.user)

        order = Order.objects.get(pk=offer.order_id)
        self.assertEqual(order.payment_route, "direct")
        self.assertEqual(order.platform_fee, Decimal("0.00"))
        # The farmer keeps the whole amount.
        self.assertEqual(order.farmer_payout, order.subtotal)
        self.assertEqual(order.total_amount, order.subtotal)

    def test_buyer_cannot_be_blocked_by_a_closed_request_after_accept(self):
        offer = self.offer()
        deals_service.respond_offer(offer, "accept", self.buyer.user)
        self.request.refresh_from_db()
        self.assertEqual(self.request.status, "fulfilled")

    def test_rival_offers_are_closed_once_a_deal_is_struck(self):
        first = self.offer(self.farmer, price="38")
        second = self.offer(self.other_farmer, price="36")
        deals_service.respond_offer(first, "accept", self.buyer.user)
        second.refresh_from_db()
        self.assertEqual(second.status, "rejected")

    def test_only_the_buyer_can_accept(self):
        offer = self.offer()
        with self.assertRaises(deals_service.DealError):
            deals_service.respond_offer(offer, "accept", self.other_farmer.user)

    def test_only_the_offering_farmer_can_withdraw(self):
        offer = self.offer()
        with self.assertRaises(deals_service.DealError):
            deals_service.withdraw_offer(offer, self.other_farmer.user)
        deals_service.withdraw_offer(offer, self.farmer.user)
        offer.refresh_from_db()
        self.assertEqual(offer.status, "withdrawn")

    def test_accepting_without_an_active_listing_is_refused(self):
        self.crop.listings.update(status="sold_out")
        offer = self.offer()
        with self.assertRaises(deals_service.DealError):
            deals_service.respond_offer(offer, "accept", self.buyer.user)

    def test_accepting_twice_is_refused(self):
        offer = self.offer()
        deals_service.respond_offer(offer, "accept", self.buyer.user)
        with self.assertRaises(deals_service.DealError):
            deals_service.respond_offer(offer, "accept", self.buyer.user)


class ContactUnlockTests(DealsTestCase):
    """The gate is deliberately asymmetric.

    A buyer who posted a request invited answers to it, so a farmer who actually
    made an offer becomes reachable. A farmer does *not* get the buyer's number
    until a deal exists — otherwise a spam offer harvests the whole board.
    """

    def test_the_buyer_can_reach_a_farmer_who_offered(self):
        deals_service.create_offer(
            self.request, self.farmer, price_per_unit=Decimal("38")
        )
        conversation = deals_service.get_or_create_conversation(self.request, self.farmer)

        buyer_view = deals_service.contact_details(conversation, self.buyer.user)
        self.assertIsNotNone(buyer_view)
        self.assertEqual(buyer_view["phone"], self.farmer.phone_number)
        self.assertEqual(buyer_view["role"], "farmer")
        self.assertTrue(buyer_view["whatsapp"].startswith("https://wa.me/"))

    def test_the_farmer_stays_locked_until_a_deal(self):
        deals_service.create_offer(
            self.request, self.farmer, price_per_unit=Decimal("38")
        )
        conversation = deals_service.get_or_create_conversation(self.request, self.farmer)

        self.assertIsNone(deals_service.contact_details(conversation, self.farmer.user))

    def test_a_rejected_offer_does_not_unlock_the_buyer(self):
        offer = deals_service.create_offer(
            self.request, self.farmer, price_per_unit=Decimal("38")
        )
        deals_service.respond_offer(offer, "reject", self.buyer.user)
        conversation = deals_service.get_or_create_conversation(self.request, self.farmer)

        self.assertIsNone(deals_service.contact_details(conversation, self.buyer.user))
        self.assertIsNone(deals_service.contact_details(conversation, self.farmer.user))

    def test_a_conversation_with_no_offer_stays_locked(self):
        conversation = deals_service.get_or_create_conversation(self.request, self.farmer)
        self.assertIsNone(deals_service.contact_details(conversation, self.buyer.user))

    def test_both_sides_are_open_once_a_deal_is_struck(self):
        offer = deals_service.create_offer(
            self.request, self.farmer, price_per_unit=Decimal("38")
        )
        conversation = deals_service.get_or_create_conversation(self.request, self.farmer)

        deals_service.respond_offer(offer, "accept", self.buyer.user)
        conversation.refresh_from_db()
        self.assertTrue(conversation.deal_struck)

        buyer_view = deals_service.contact_details(conversation, self.buyer.user)
        farmer_view = deals_service.contact_details(conversation, self.farmer.user)
        self.assertEqual(buyer_view["phone"], self.farmer.phone_number)
        self.assertEqual(farmer_view["phone"], self.buyer.phone_number)

    def test_a_name_is_never_just_a_phone_number(self):
        offer = deals_service.create_offer(
            self.request, self.farmer, price_per_unit=Decimal("38")
        )
        deals_service.respond_offer(offer, "accept", self.buyer.user)
        conversation = Conversation.objects.get(request=self.request)

        farmer_view = deals_service.contact_details(conversation, self.farmer.user)
        self.assertNotEqual(farmer_view["name"], self.buyer.phone_number)
        self.assertTrue(farmer_view["name"])

    def test_outsiders_get_no_contact_details(self):
        offer = deals_service.create_offer(
            self.request, self.farmer, price_per_unit=Decimal("38")
        )
        deals_service.respond_offer(offer, "accept", self.buyer.user)
        conversation = Conversation.objects.get(request=self.request)
        stranger = make_buyer(username="nosybuyer").user
        self.assertIsNone(deals_service.contact_details(conversation, stranger))


class OrderContactTests(DealsTestCase):
    """An order is the deal, so both sides can reach each other."""

    def setUp(self):
        super().setUp()
        self.listing = make_listing(
            farmer=self.farmer, crop=self.crop, quantity="100", price="40"
        )
        self.order = order_service.create_order(self.buyer, self.listing, Decimal("5"))

    def test_the_buyer_sees_the_farmer(self):
        contact = deals_service.order_contact(self.order, self.buyer.user)
        self.assertEqual(contact["role"], "farmer")
        self.assertEqual(contact["phone"], self.farmer.phone_number)
        self.assertEqual(contact["village"], self.farmer.village)

    def test_the_farmer_sees_the_buyer(self):
        contact = deals_service.order_contact(self.order, self.farmer.user)
        self.assertEqual(contact["role"], "buyer")
        self.assertEqual(contact["phone"], self.buyer.phone_number)

    def test_outsiders_see_nothing(self):
        stranger = make_buyer(username="orderstranger").user
        self.assertIsNone(deals_service.order_contact(self.order, stranger))

    def test_the_order_page_renders_the_contact(self):
        self.client.force_login(self.buyer.user)
        response = self.client.get(
            reverse("marketplace:order_detail", args=[self.order.pk])
        )
        self.assertContains(response, "Contact your farmer")
        self.assertContains(response, self.farmer.phone_number)
        self.assertContains(response, "wa.me")


class ConversationTests(DealsTestCase):
    def setUp(self):
        super().setUp()
        deals_service.create_offer(self.request, self.farmer, price_per_unit=Decimal("38"))
        self.conversation = Conversation.objects.get(request=self.request)

    def test_both_sides_can_post(self):
        deals_service.post_message(self.conversation, self.buyer.user, "Is it organic?")
        deals_service.post_message(self.conversation, self.farmer.user, "Yes, no chemicals.")
        self.assertEqual(self.conversation.messages.count(), 2)
        self.assertEqual(self.conversation.last_message_at is not None, True)

    def test_outsider_cannot_post(self):
        stranger = make_buyer(username="stranger").user
        with self.assertRaises(deals_service.DealError):
            deals_service.post_message(self.conversation, stranger, "hello")

    def test_empty_message_is_refused(self):
        with self.assertRaises(deals_service.DealError):
            deals_service.post_message(self.conversation, self.buyer.user, "   ")

    def test_offer_message_seeds_the_thread(self):
        offer = deals_service.create_offer(
            self.request, self.other_farmer, price_per_unit=Decimal("36"),
            message="I can deliver tomorrow",
        )
        conversation = Conversation.objects.get(request=self.request, farmer=self.other_farmer)
        self.assertEqual(conversation.messages.count(), 1)
        self.assertEqual(conversation.messages.first().body, "I can deliver tomorrow")


class DirectPaymentTests(DealsTestCase):
    def setUp(self):
        super().setUp()
        deals_service.save_payout_method(self.farmer.user, kind="upi", upi_id="farmer@okaxis")
        self.order = order_service.create_order(
            self.buyer,
            self.crop.listings.filter(farmer=self.farmer).first(),
            Decimal("10"),
            price=Decimal("40"),
            payment_route="direct",
        )

    def test_link_pays_the_farmer_and_carries_no_fee(self):
        link = upi.build_seller_upi_link(self.order, "farmer@okaxis", "Test Farmer")
        self.assertIn("pa=farmer%40okaxis", link)
        self.assertIn(f"am={self.order.subtotal}", link)

    def test_invalid_vpa_is_refused(self):
        with self.assertRaises(ValueError):
            upi.build_seller_upi_link(self.order, "not-a-vpa")

    def test_buyer_records_utr_without_settling(self):
        payment = upi.record_seller_payment(self.order, "UTR778899")
        self.assertEqual(payment.provider, upi.FARMER_PROVIDER)
        self.assertEqual(payment.status, "authorized")
        self.order.refresh_from_db()
        self.assertEqual(self.order.payment_status, "pending")

    def test_farmer_confirms_and_order_settles(self):
        payment = upi.record_seller_payment(self.order, "UTR778899")
        upi.confirm_seller_receipt(payment, self.farmer.user)
        payment.refresh_from_db()
        self.assertEqual(payment.status, "captured")
        self.order.refresh_from_db()
        self.assertEqual(self.order.payment_status, "paid")

    def test_a_stranger_cannot_confirm_receipt(self):
        payment = upi.record_seller_payment(self.order, "UTR778899")
        with self.assertRaises(upi.UpiError):
            upi.confirm_seller_receipt(payment, self.other_farmer.user)
        self.order.refresh_from_db()
        self.assertEqual(self.order.payment_status, "pending")

    def test_platform_orders_reject_the_direct_flow(self):
        platform_order = order_service.create_order(
            self.buyer,
            self.crop.listings.filter(farmer=self.farmer).first(),
            Decimal("2"),
            price=Decimal("40"),
        )
        with self.assertRaises(upi.UpiError):
            upi.record_seller_payment(platform_order, "UTR000111")

    def test_duplicate_claim_is_refused(self):
        upi.record_seller_payment(self.order, "UTR778899")
        with self.assertRaises(upi.UpiError):
            upi.record_seller_payment(self.order, "UTR000222")


class PayoutMethodTests(TestCase):
    def setUp(self):
        self.farmer = make_farmer(username="payoutfarmer")

    def test_saving_a_upi_id_enables_direct_payments(self):
        self.assertFalse(deals_service.can_take_direct_payments(self.farmer))
        deals_service.save_payout_method(self.farmer.user, kind="upi", upi_id="me@ybl")
        self.assertTrue(deals_service.can_take_direct_payments(self.farmer))

    def test_bank_details_are_stored_but_masked(self):
        method = deals_service.save_payout_method(
            self.farmer.user, kind="bank",
            account_holder="Test Farmer", account_number="123456789012", ifsc="HDFC0001234",
        )
        self.assertEqual(method.account_number, "123456789012")
        self.assertEqual(method.masked_account_number, "XXXX9012")
        self.assertNotIn("123456789012", method.display)
        self.assertFalse(deals_service.can_take_direct_payments(self.farmer))

    def test_invalid_details_are_rejected(self):
        for kwargs in (
            {"kind": "upi", "upi_id": "nope"},
            {"kind": "upi", "upi_id": ""},
            {"kind": "bank", "account_holder": "", "account_number": "123456", "ifsc": "HDFC0001234"},
            {"kind": "bank", "account_holder": "A", "account_number": "12", "ifsc": "HDFC0001234"},
            {"kind": "bank", "account_holder": "A", "account_number": "123456789", "ifsc": "bad"},
        ):
            with self.assertRaises(deals_service.DealError):
                deals_service.save_payout_method(self.farmer.user, **kwargs)


@override_settings(PLATFORM_FEE_PERCENT="2.0")
class FeeRuleTests(TestCase):
    def test_direct_orders_carry_no_platform_fee(self):
        farmer = make_farmer(username="feefarmer")
        buyer = make_buyer(username="feebuyer")
        crop = make_crop("Chilli")
        listing = make_listing(farmer=farmer, crop=crop, quantity="100", price="100")

        platform_order = order_service.create_order(buyer, listing, Decimal("10"))
        direct_order = order_service.create_order(
            buyer, listing, Decimal("10"), payment_route="direct"
        )

        self.assertEqual(platform_order.platform_fee, Decimal("20.00"))
        self.assertEqual(direct_order.platform_fee, Decimal("0.00"))
        self.assertEqual(
            order_service.calculate_platform_fee(Decimal("1000"), "direct"), Decimal("0.00")
        )


class DealsWebTests(DealsTestCase):
    def test_buyer_posts_a_request_through_the_form(self):
        self.client.force_login(self.buyer.user)
        response = self.client.post(
            reverse("marketplace:request_create"),
            {
                "crop": self.crop.pk,
                "quantity": "50",
                "target_price": "30",
                "delivery_city": "Mumbai",
                "notes": "Organic please",
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assertTrue(DemandRequest.objects.filter(delivery_city="Mumbai").exists())

    def test_farmer_cannot_post_a_request(self):
        self.client.force_login(self.farmer.user)
        response = self.client.post(
            reverse("marketplace:request_create"),
            {"crop": self.crop.pk, "quantity": "5", "delivery_city": "Pune"},
        )
        self.assertEqual(response.status_code, 403)

    def test_board_shows_open_requests_to_a_farmer(self):
        self.client.force_login(self.farmer.user)
        response = self.client.get(reverse("marketplace:request_list"))
        self.assertContains(response, "Tomato")

    def test_seller_only_sees_their_own_offer_on_a_request(self):
        deals_service.create_offer(self.request, self.farmer, price_per_unit=Decimal("38"))
        deals_service.create_offer(
            self.request, self.other_farmer, price_per_unit=Decimal("36")
        )
        self.client.force_login(self.farmer.user)
        response = self.client.get(
            reverse("marketplace:request_detail", args=[self.request.pk])
        )
        self.assertContains(response, "38.00")
        self.assertNotContains(response, "36.00")

    def test_buyer_sees_every_offer_on_their_request(self):
        deals_service.create_offer(self.request, self.farmer, price_per_unit=Decimal("38"))
        deals_service.create_offer(
            self.request, self.other_farmer, price_per_unit=Decimal("36")
        )
        self.client.force_login(self.buyer.user)
        response = self.client.get(
            reverse("marketplace:request_detail", args=[self.request.pk])
        )
        self.assertContains(response, "38.00")
        self.assertContains(response, "36.00")

    def test_buyer_can_accept_and_land_on_the_order(self):
        offer = deals_service.create_offer(
            self.request, self.farmer, price_per_unit=Decimal("38")
        )
        self.client.force_login(self.buyer.user)
        response = self.client.post(
            reverse("marketplace:request_offer_action", args=[offer.pk, "accept"])
        )
        offer.refresh_from_db()
        self.assertRedirects(response, reverse("marketplace:order_detail", args=[offer.order_id]))

    def test_direct_route_is_hidden_without_a_upi_id(self):
        self.client.force_login(self.farmer.user)
        response = self.client.get(
            reverse("marketplace:request_detail", args=[self.request.pk])
        )
        self.assertNotContains(response, "Pay me directly")
        self.assertContains(response, "Through the platform")

    def test_conversation_page_is_private(self):
        offer = deals_service.create_offer(
            self.request, self.farmer, price_per_unit=Decimal("38")
        )
        conversation = Conversation.objects.get(request=self.request)
        self.client.force_login(make_buyer(username="eavesdropper").user)
        response = self.client.get(reverse("marketplace:conversation", args=[conversation.pk]))
        self.assertEqual(response.status_code, 302)

    def test_payout_page_saves_a_upi_id(self):
        self.client.force_login(self.farmer.user)
        response = self.client.post(
            reverse("marketplace:payout_methods"),
            {"kind": "upi", "upi_id": "farmer@ybl", "account_holder": "",
             "account_number": "", "ifsc": ""},
        )
        self.assertEqual(response.status_code, 302)
        self.assertTrue(PayoutMethod.objects.filter(user=self.farmer.user, upi_id="farmer@ybl").exists())

    def test_payout_page_rejects_a_bad_vpa(self):
        self.client.force_login(self.farmer.user)
        self.client.post(
            reverse("marketplace:payout_methods"),
            {"kind": "upi", "upi_id": "nope", "account_holder": "",
             "account_number": "", "ifsc": ""},
        )
        self.assertFalse(PayoutMethod.objects.filter(user=self.farmer.user).exists())
"""Tests for contacting a seller directly from a listing.

Before this, the only route into messaging was the demand board: a buyer had
to post a public need and wait for a farmer to answer it. A buyer browsing a
listing with a simple question had no way to ask it.
"""

from decimal import Decimal

from django.test import TestCase
from django.urls import reverse

from marketplace.models import Conversation
from marketplace.services import deals as deals_service
from marketplace.tests.base import CacheResetTestCase
from marketplace.tests.factories import make_buyer, make_crop, make_farmer, make_listing


class ContactSellerTests(CacheResetTestCase):
    def setUp(self):
        super().setUp()
        self.farmer = make_farmer(username="contact_farmer")
        self.buyer = make_buyer(username="contact_buyer")
        self.listing = make_listing(
            farmer=self.farmer, crop=make_crop("Tomato"), quantity="100", price="40"
        )
        self.client.force_login(self.buyer.user)

    def post_contact(self, listing=None):
        return self.client.post(
            reverse("marketplace:contact_seller", args=[(listing or self.listing).pk])
        )

    # -- the happy path ------------------------------------------------------

    def test_a_buyer_can_open_a_thread_from_a_listing(self):
        response = self.post_contact()
        conversation = Conversation.objects.get()
        self.assertRedirects(response, reverse("marketplace:conversation", args=[conversation.pk]))
        self.assertEqual(conversation.buyer_id, self.buyer.id)
        self.assertEqual(conversation.farmer_id, self.farmer.id)
        self.assertEqual(conversation.listing_id, self.listing.pk)
        self.assertIsNone(conversation.request_id)

    def test_the_seller_sees_the_thread_in_their_inbox(self):
        self.post_contact()
        conversation = Conversation.objects.get()
        for conversation_for in deals_service.conversations_for(self.farmer.user):
            self.assertEqual(conversation_for.pk, conversation.pk)

    def test_both_sides_can_reply_in_a_listing_thread(self):
        self.post_contact()
        conversation = Conversation.objects.get()
        deals_service.post_message(conversation, self.buyer.user, "Is this from your own field?")
        deals_service.post_message(conversation, self.farmer.user, "Yes, picked this morning.")
        self.assertEqual(conversation.messages.count(), 2)

    def test_clicking_twice_rejoins_the_same_thread(self):
        """A second click must not create a second conversation with the same seller."""
        self.post_contact()
        conversation = Conversation.objects.get()
        self.post_contact()
        self.assertEqual(Conversation.objects.count(), 1)
        self.assertEqual(Conversation.objects.get().pk, conversation.pk)

    def test_the_listing_page_offers_the_button(self):
        response = self.client.get(
            reverse("marketplace:listing_detail", args=[self.listing.pk])
        )
        self.assertContains(response, reverse("marketplace:contact_seller", args=[self.listing.pk]))

    def test_the_thread_says_which_listing_it_is_about(self):
        self.post_contact()
        conversation = Conversation.objects.get()
        response = self.client.get(reverse("marketplace:conversation", args=[conversation.pk]))
        self.assertContains(response, "About the listing")

    # -- who may not --------------------------------------------------------

    def test_the_seller_cannot_message_themselves(self):
        self.client.force_login(self.farmer.user)
        response = self.post_contact()
        self.assertEqual(Conversation.objects.count(), 0)
        self.assertEqual(response.status_code, 302)

    def test_an_anonymous_visitor_is_sent_to_login(self):
        self.client.logout()
        response = self.post_contact()
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("marketplace:login"), response.url)
        self.assertEqual(Conversation.objects.count(), 0)

    def test_a_stranger_cannot_read_someone_elses_thread(self):
        self.post_contact()
        conversation = Conversation.objects.get()
        deals_service.post_message(conversation, self.buyer.user, "secret")
        intruder = make_buyer(username="intruder")
        self.client.force_login(intruder.user)
        response = self.client.get(reverse("marketplace:conversation", args=[conversation.pk]))
        # Bounced to the dashboard rather than shown the thread.
        self.assertEqual(response.status_code, 302)
        self.assertNotContains(response, "secret", status_code=302)

    def test_contacting_a_sold_out_listing_is_still_allowed(self):
        """Someone asking whether more is coming is exactly when this matters."""
        self.listing.status = "sold_out"
        self.listing.save(update_fields=["status"])
        self.post_contact()
        self.assertEqual(Conversation.objects.count(), 1)


class ContactDetailGateTests(CacheResetTestCase):
    """A listing thread must not weaken the existing contact gate.

    Contact details are hidden until there is a real order, and a thread opened
    from a listing is still just a question — it must not become a way around
    that, in either direction.
    """

    def setUp(self):
        super().setUp()
        self.farmer = make_farmer(username="gate_farmer")
        self.buyer = make_buyer(username="gate_buyer")
        self.listing = make_listing(
            farmer=self.farmer, crop=make_crop("Onion"), quantity="50", price="25"
        )
        self.conversation = deals_service.get_or_create_listing_conversation(
            self.listing, self.buyer
        )

    def test_the_buyer_cannot_see_the_farmer_number_before_an_order(self):
        self.assertIsNone(deals_service.contact_details(self.conversation, self.buyer.user))

    def test_the_farmer_cannot_see_the_buyer_number_before_an_order(self):
        self.assertIsNone(deals_service.contact_details(self.conversation, self.farmer.user))

    def test_an_order_unlocks_both_sides(self):
        from marketplace.services import orders as order_service

        order = order_service.create_order(self.buyer, self.listing, Decimal("10"))
        self.conversation.order = order
        self.conversation.save(update_fields=["order"])

        buyer_view = deals_service.contact_details(self.conversation, self.buyer.user)
        farmer_view = deals_service.contact_details(self.conversation, self.farmer.user)
        self.assertEqual(buyer_view["phone"], self.farmer.phone_number)
        self.assertEqual(farmer_view["phone"], self.buyer.phone_number)
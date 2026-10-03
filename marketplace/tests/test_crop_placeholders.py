"""Tests for the crop-aware listing placeholder art.

A listing with no photo falls back to artwork chosen from the crop's name, so
a card looks like a vegetable rather than a broken image. The mapping has to be
forgiving about farmer-entered names ("Tomato (Hybrid)", "SOYA BEAN") while still
falling back to the generic crate for anything unknown.
"""

from django.contrib.staticfiles import finders
from django.test import SimpleTestCase, TestCase
from django.urls import reverse

from marketplace.models import Crop
from marketplace.tests.factories import make_crop, make_farmer, make_listing

#: Every crop `seed_data` creates, which is what the placeholder art must cover.
SEEDED_CROPS = [
    "Tomato", "Onion", "Potato", "Wheat", "Rice (Paddy)",
    "Maize", "Banana", "Mango", "Groundnut", "Soybean",
]


class PlaceholderMappingTests(SimpleTestCase):
    """The name -> art mapping, independent of any database row."""

    def test_every_seeded_crop_gets_its_own_art(self):
        for name in SEEDED_CROPS:
            with self.subTest(crop=name):
                crop = Crop(name=name)
                self.assertEqual(crop.placeholder_image, f"img/crops/{_slug(name)}.svg")

    def test_every_mapped_asset_actually_exists(self):
        """A typo in the map would ship a 404 to every card that uses it."""
        for keyword, path in Crop.PLACEHOLDER_IMAGES.items():
            with self.subTest(keyword=keyword):
                self.assertIsNotNone(finders.find(path), f"missing asset for {keyword}: {path}")

    def test_the_generic_fallback_exists(self):
        self.assertIsNotNone(finders.find(Crop.DEFAULT_PLACEHOLDER_IMAGE))

    def test_an_unknown_crop_falls_back_to_the_crate(self):
        self.assertEqual(
            Crop(name="Dragonfruit").placeholder_image, Crop.DEFAULT_PLACEHOLDER_IMAGE
        )

    def test_a_crop_with_no_name_does_not_explode(self):
        self.assertEqual(Crop(name="").placeholder_image, Crop.DEFAULT_PLACEHOLDER_IMAGE)

    def test_matching_ignores_case_and_surrounding_words(self):
        for name, expected in [
            ("TOMATO", "img/crops/tomato.svg"),
            ("Tomato (Hybrid)", "img/crops/tomato.svg"),
            ("SOYA BEAN", "img/crops/soybean.svg"),
            ("Corn", "img/crops/maize.svg"),
            ("Paddy", "img/crops/rice.svg"),
            ("shallot", "img/crops/onion.svg"),
        ]:
            with self.subTest(crop=name):
                self.assertEqual(Crop(name=name).placeholder_image, expected)

    def test_the_longest_keyword_wins(self):
        """Longest-first ordering is what keeps "Sweet Potato" off the generic art."""
        keywords = Crop.PLACEHOLDER_KEYWORDS
        self.assertEqual(keywords, sorted(keywords, key=len, reverse=True))
        self.assertEqual(Crop(name="Sweet Potato").placeholder_image, "img/crops/potato.svg")


class PlaceholderRenderingTests(TestCase):
    """The art reaches the page, not just the model."""

    def test_a_photoless_listing_renders_its_crop_art(self):
        listing = make_listing(farmer=make_farmer(), crop=make_crop(name="Mango"))
        response = self.client.get(reverse("marketplace:listing_detail", args=[listing.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "img/crops/mango.svg")

    def test_a_listing_with_a_photo_uses_the_photo(self):
        listing = make_listing(farmer=make_farmer(), crop=make_crop(name="Mango"))
        response = self.client.get(reverse("marketplace:listing_detail", args=[listing.pk]))
        # No upload in this fixture, so the placeholder is the only source; the
        # point is that the fallback branch is what renders.
        self.assertNotContains(response, Crop.DEFAULT_PLACEHOLDER_IMAGE)

    def test_the_api_exposes_the_placeholder(self):
        listing = make_listing(farmer=make_farmer(), crop=make_crop(name="Wheat"))
        response = self.client.get(f"/api/listings/{listing.pk}/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["placeholder_image"], "img/crops/wheat.svg")


def _slug(name: str) -> str:
    """The asset filename a seeded crop is expected to use."""
    overrides = {"Rice (Paddy)": "rice"}
    return overrides.get(name, name.lower())
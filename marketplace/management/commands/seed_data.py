"""Populate the database with reference crops and optional demo data.

Usage:
    python manage.py seed_data            # crops only (safe in production)
    python manage.py seed_data --demo     # crops + a farmer, buyer and listings
"""

import random
from datetime import timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand
from django.utils import timezone

from marketplace.models import BuyerProfile, Crop, FarmerProfile, Listing

User = get_user_model()

CROPS = [
    ("Tomato", "kg", "Vegetables"),
    ("Onion", "kg", "Vegetables"),
    ("Potato", "kg", "Vegetables"),
    ("Wheat", "quintal", "Cereals"),
    ("Rice (Paddy)", "quintal", "Cereals"),
    ("Maize", "quintal", "Cereals"),
    ("Banana", "dozen", "Fruits"),
    ("Mango", "kg", "Fruits"),
    ("Groundnut", "quintal", "Oilseeds"),
    ("Soybean", "quintal", "Oilseeds"),
]


class Command(BaseCommand):
    help = "Seed reference data (and optionally demo users/listings)."

    def add_arguments(self, parser):
        parser.add_argument("--demo", action="store_true", help="Also create demo accounts and listings")

    def handle(self, *args, **options):
        created = 0
        for name, unit, category in CROPS:
            _, was_created = Crop.objects.get_or_create(
                name=name, defaults={"unit": unit, "category": category}
            )
            created += int(was_created)
        self.stdout.write(self.style.SUCCESS(f"Crops ready ({created} new)."))

        if not options["demo"]:
            return

        farmer_user, _ = User.objects.get_or_create(
            username="demo_farmer",
            defaults={"email": "farmer@example.com", "first_name": "Ravi", "last_name": "Patil"},
        )
        farmer_user.set_password("DemoPass123")
        farmer_user.save()
        farmer, _ = FarmerProfile.objects.get_or_create(
            user=farmer_user,
            defaults={
                "full_name": "Ravi Patil",
                "phone_number": "9000000001",
                "village": "Shirur",
                "district": "Pune",
                "state": "Maharashtra",
                "latitude": Decimal("18.528200"),
                "longitude": Decimal("74.002100"),
                "kyc_verified": True,
            },
        )

        buyer_user, _ = User.objects.get_or_create(
            username="demo_buyer",
            defaults={"email": "buyer@example.com", "first_name": "Anita", "last_name": "Desai"},
        )
        buyer_user.set_password("DemoPass123")
        buyer_user.save()
        BuyerProfile.objects.get_or_create(
            user=buyer_user,
            defaults={
                "buyer_type": "bulk",
                "business_name": "Fresh Mart",
                "phone_number": "9000000002",
                "city": "Mumbai",
                "address": "Dadar Market, Mumbai",
                "latitude": Decimal("19.018000"),
                "longitude": Decimal("72.844000"),
            },
        )

        rng = random.Random(42)
        for crop in Crop.objects.all()[:6]:
            Listing.objects.get_or_create(
                farmer=farmer,
                crop=crop,
                defaults={
                    "quantity_available": Decimal(rng.randint(80, 600)),
                    "quality_grade": rng.choice(["A", "A", "B"]),
                    "price_per_unit": Decimal(rng.randint(15, 220)),
                    "harvest_date": timezone.now().date() - timedelta(days=rng.randint(0, 5)),
                    "description": f"Fresh {crop.name} from Pune district.",
                    "is_organic": rng.random() < 0.3,
                },
            )
        self.stdout.write(self.style.SUCCESS("Demo data ready. Login: demo_farmer / demo_buyer (DemoPass123)"))

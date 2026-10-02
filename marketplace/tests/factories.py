"""Small helpers for constructing test fixtures without extra dependencies."""

from decimal import Decimal

from django.contrib.auth import get_user_model

from marketplace.models import BuyerProfile, Crop, FarmerProfile, Listing

User = get_user_model()


def make_farmer(username="farmer1", **profile_kwargs):
    user = User.objects.create_user(username=username, password="testpass123", email=f"{username}@example.com")
    defaults = {
        "full_name": "Test Farmer",
        "phone_number": f"90000{abs(hash(username)) % 100000:05d}",
        "village": "Shirur",
        "district": "Pune",
        "state": "Maharashtra",
        "latitude": Decimal("18.528200"),
        "longitude": Decimal("74.002100"),
    }
    defaults.update(profile_kwargs)
    return FarmerProfile.objects.create(user=user, **defaults)


def make_buyer(username="buyer1", **profile_kwargs):
    user = User.objects.create_user(username=username, password="testpass123", email=f"{username}@example.com")
    defaults = {
        "buyer_type": "consumer",
        "phone_number": f"80000{abs(hash(username)) % 100000:05d}",
        "city": "Mumbai",
        "latitude": Decimal("19.018000"),
        "longitude": Decimal("72.844000"),
    }
    defaults.update(profile_kwargs)
    return BuyerProfile.objects.create(user=user, **defaults)


def make_crop(name="Tomato", unit="kg"):
    return Crop.objects.get_or_create(name=name, defaults={"unit": unit})[0]


def make_staff(username="staff1"):
    """A staff account with every marketplace permission (admin actions need them)."""
    from django.contrib.auth.models import Permission

    user = User.objects.create_user(
        username=username, password="testpass123", email=f"{username}@example.com",
        is_staff=True,
    )
    user.user_permissions.set(
        Permission.objects.filter(content_type__app_label="marketplace")
    )
    return user


def make_listing(farmer=None, crop=None, quantity="100.00", price="40.00", **kwargs):
    farmer = farmer or make_farmer()
    crop = crop or make_crop()
    from django.utils import timezone

    return Listing.objects.create(
        farmer=farmer,
        crop=crop,
        quantity_available=Decimal(quantity),
        price_per_unit=Decimal(price),
        harvest_date=timezone.now().date(),
        **kwargs,
    )

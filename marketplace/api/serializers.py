"""DRF serializers for the marketplace API."""

from django.contrib.auth import get_user_model
from django.db import transaction
from rest_framework import serializers

from marketplace.models import (
    BuyerProfile,
    Crop,
    DeliveryPartner,
    DemandForecast,
    FarmerProfile,
    Listing,
    Order,
    OrderStatusHistory,
    Payment,
    PriceOffer,
    Shipment,
    ShipmentStop,
)
from marketplace.services import offers as offer_service
from marketplace.services import orders as order_service
from marketplace.services.offers import OfferError

User = get_user_model()


# ---------------------------------------------------------------------------
# Accounts
# ---------------------------------------------------------------------------
class UserSerializer(serializers.ModelSerializer):
    class Meta:
        model = User
        fields = ["id", "username", "email", "first_name", "last_name"]
        read_only_fields = fields


class RegisterSerializer(serializers.ModelSerializer):
    """Create a user together with the matching farmer or buyer profile."""

    password = serializers.CharField(write_only=True, min_length=8)
    role = serializers.ChoiceField(choices=["farmer", "buyer"], write_only=True)
    full_name = serializers.CharField(write_only=True)
    phone_number = serializers.CharField(write_only=True)
    is_fpo = serializers.BooleanField(write_only=True, required=False, default=False)
    village = serializers.CharField(write_only=True, required=False, allow_blank=True)
    district = serializers.CharField(write_only=True, required=False, allow_blank=True)
    state = serializers.CharField(write_only=True, required=False, allow_blank=True)
    buyer_type = serializers.ChoiceField(
        choices=BuyerProfile.BUYER_TYPE_CHOICES, write_only=True, required=False
    )
    business_name = serializers.CharField(write_only=True, required=False, allow_blank=True)
    city = serializers.CharField(write_only=True, required=False, allow_blank=True)

    class Meta:
        model = User
        fields = [
            "id", "username", "email", "password", "role", "full_name",
            "phone_number", "is_fpo", "village", "district", "state",
            "buyer_type", "business_name", "city",
        ]
        read_only_fields = ["id"]

    def validate_email(self, value):
        if User.objects.filter(email__iexact=value).exists():
            raise serializers.ValidationError("An account with this email already exists.")
        return value.lower()

    def validate(self, attrs):
        if attrs["role"] == "farmer":
            missing = [f for f in ("village", "district", "state") if not attrs.get(f)]
            if missing:
                raise serializers.ValidationError(
                    {f: "Required for farmers." for f in missing}
                )
        else:
            missing = [f for f in ("buyer_type", "city") if not attrs.get(f)]
            if missing:
                raise serializers.ValidationError(
                    {f: "Required for buyers." for f in missing}
                )
        return attrs

    @transaction.atomic
    def create(self, validated_data):
        role = validated_data.pop("role")
        password = validated_data.pop("password")
        profile_fields = {
            key: validated_data.pop(key, None)
            for key in (
                "full_name", "phone_number", "is_fpo", "village", "district",
                "state", "buyer_type", "business_name", "city",
            )
        }
        user = User.objects.create_user(password=password, **validated_data)
        if role == "farmer":
            FarmerProfile.objects.create(
                user=user,
                full_name=profile_fields["full_name"],
                phone_number=profile_fields["phone_number"],
                is_fpo=profile_fields.get("is_fpo") or False,
                village=profile_fields["village"],
                district=profile_fields["district"],
                state=profile_fields["state"],
            )
        else:
            BuyerProfile.objects.create(
                user=user,
                buyer_type=profile_fields["buyer_type"],
                business_name=profile_fields.get("business_name") or "",
                phone_number=profile_fields["phone_number"],
                city=profile_fields["city"],
            )
        return user


class FarmerProfileSerializer(serializers.ModelSerializer):
    user = UserSerializer(read_only=True)

    class Meta:
        model = FarmerProfile
        fields = [
            "id", "user", "is_fpo", "full_name", "phone_number", "village",
            "district", "state", "preferred_language", "latitude", "longitude",
            "kyc_verified", "created_at",
        ]
        read_only_fields = ["id", "created_at", "kyc_verified"]


class BuyerProfileSerializer(serializers.ModelSerializer):
    user = UserSerializer(read_only=True)

    class Meta:
        model = BuyerProfile
        fields = [
            "id", "user", "buyer_type", "business_name", "phone_number",
            "city", "address", "latitude", "longitude", "created_at",
        ]
        read_only_fields = ["id", "created_at"]


# ---------------------------------------------------------------------------
# Supply
# ---------------------------------------------------------------------------
class CropSerializer(serializers.ModelSerializer):
    class Meta:
        model = Crop
        fields = ["id", "name", "unit", "category"]


class ListingSerializer(serializers.ModelSerializer):
    crop_name = serializers.CharField(source="crop.name", read_only=True)
    crop_unit = serializers.CharField(source="crop.unit", read_only=True)
    farmer_name = serializers.CharField(source="farmer.full_name", read_only=True)
    farmer_village = serializers.CharField(source="farmer.village", read_only=True)
    is_orderable = serializers.BooleanField(read_only=True)
    placeholder_image = serializers.CharField(source="crop.placeholder_image", read_only=True)

    class Meta:
        model = Listing
        fields = [
            "id", "farmer", "crop", "crop_name", "crop_unit", "quantity_available",
            "quality_grade", "price_per_unit", "ai_suggested_price", "harvest_date",
            "photo", "description", "is_organic", "status", "created_at",
            "farmer_name", "farmer_village", "is_orderable", "placeholder_image",
        ]
        read_only_fields = ["id", "created_at", "status", "farmer", "ai_suggested_price"]

    def validate_quantity_available(self, value):
        if value <= 0:
            raise serializers.ValidationError("Quantity must be greater than zero.")
        return value

    def validate_price_per_unit(self, value):
        if value <= 0:
            raise serializers.ValidationError("Price must be greater than zero.")
        return value


# ---------------------------------------------------------------------------
# Demand
# ---------------------------------------------------------------------------
class OrderStatusHistorySerializer(serializers.ModelSerializer):
    changed_by = serializers.StringRelatedField()

    class Meta:
        model = OrderStatusHistory
        fields = ["id", "order", "status", "note", "changed_by", "changed_at"]
        read_only_fields = ["id", "changed_at"]


class PaymentSerializer(serializers.ModelSerializer):
    class Meta:
        model = Payment
        fields = [
            "id", "order", "provider", "provider_order_id", "provider_payment_id",
            "amount", "currency", "status", "method", "failure_reason", "created_at",
        ]
        read_only_fields = [
            "id", "provider", "provider_order_id", "provider_payment_id",
            "amount", "currency", "status", "method", "failure_reason", "created_at",
        ]


class OrderSerializer(serializers.ModelSerializer):
    crop_name = serializers.CharField(source="listing.crop.name", read_only=True)
    crop_unit = serializers.CharField(source="listing.crop.unit", read_only=True)
    buyer_name = serializers.SerializerMethodField()
    farmer_name = serializers.CharField(source="listing.farmer.full_name", read_only=True)
    listing_price = serializers.DecimalField(
        source="listing.price_per_unit", max_digits=10, decimal_places=2, read_only=True
    )
    status_history = OrderStatusHistorySerializer(many=True, read_only=True)
    payments = PaymentSerializer(many=True, read_only=True)
    total_amount = serializers.DecimalField(max_digits=12, decimal_places=2, read_only=True)
    farmer_payout = serializers.DecimalField(max_digits=12, decimal_places=2, read_only=True)

    class Meta:
        model = Order
        fields = [
            "id", "buyer", "listing", "quantity_ordered", "agreed_price_per_unit",
            "platform_fee", "delivery_fee", "status", "payment_status", "pickup_hub",
            "delivery_address", "delivery_latitude", "delivery_longitude",
            "created_at", "updated_at", "total_amount", "farmer_payout",
            "crop_name", "crop_unit", "buyer_name", "farmer_name", "listing_price",
            "status_history", "payments",
        ]
        read_only_fields = [
            "id", "buyer", "platform_fee", "delivery_fee", "status", "payment_status",
            "pickup_hub", "created_at", "updated_at", "total_amount", "farmer_payout",
            "buyer_name", "farmer_name", "status_history", "payments",
            # The buyer no longer sets their own price — it comes from the
            # listing, or from an offer the farmer accepted.
            "agreed_price_per_unit",
        ]

    def get_buyer_name(self, obj):
        return str(obj.buyer)

    def validate(self, attrs):
        request = self.context.get("request")
        listing = attrs.get("listing")
        buyer = getattr(request.user, "buyer_profile", None) if request else None
        if buyer is None:
            raise serializers.ValidationError("Only buyers can place orders.")
        if listing and listing.farmer.user_id == request.user.id:
            raise serializers.ValidationError("You cannot order your own listing.")
        if listing and not listing.is_orderable:
            raise serializers.ValidationError("This listing is not available.")
        quantity = attrs.get("quantity_ordered")
        if listing and quantity and quantity > listing.quantity_available:
            raise serializers.ValidationError(
                {"quantity_ordered": f"Only {listing.quantity_available} available."}
            )
        return attrs

    def create(self, validated_data):
        request = self.context["request"]
        try:
            return order_service.create_order(
                buyer=request.user.buyer_profile,
                listing=validated_data["listing"],
                quantity=validated_data["quantity_ordered"],
                price=validated_data.get("agreed_price_per_unit"),
            )
        except order_service.OrderError as exc:
            raise serializers.ValidationError(str(exc))


# ---------------------------------------------------------------------------
# Logistics
# ---------------------------------------------------------------------------
class DeliveryPartnerSerializer(serializers.ModelSerializer):
    class Meta:
        model = DeliveryPartner
        fields = [
            "id", "name", "phone_number", "vehicle_type", "capacity_kg",
            "is_available", "current_latitude", "current_longitude",
        ]


class ShipmentStopSerializer(serializers.ModelSerializer):
    crop_name = serializers.CharField(source="order.listing.crop.name", read_only=True)

    class Meta:
        model = ShipmentStop
        fields = [
            "id", "kind", "sequence", "label", "latitude", "longitude",
            "status", "completed_at", "order", "crop_name",
        ]


class ShipmentSerializer(serializers.ModelSerializer):
    stops = ShipmentStopSerializer(many=True, read_only=True)
    partner_name = serializers.CharField(source="partner.name", read_only=True)

    class Meta:
        model = Shipment
        fields = [
            "id", "partner", "partner_name", "status", "planned_distance_km",
            "planned_cost", "optimized_at", "route_notes", "created_at", "stops",
        ]
        read_only_fields = fields


class PlanShipmentSerializer(serializers.Serializer):
    """Input for the shipment-planning action."""

    order_ids = serializers.ListField(child=serializers.IntegerField(), allow_empty=False)
    partner_id = serializers.IntegerField(required=False, allow_null=True)


# ---------------------------------------------------------------------------
# Intelligence
# ---------------------------------------------------------------------------
class DemandForecastSerializer(serializers.ModelSerializer):
    crop_name = serializers.CharField(source="crop.name", read_only=True)

    class Meta:
        model = DemandForecast
        fields = [
            "id", "crop", "crop_name", "region", "horizon_days",
            "predicted_quantity", "predicted_price_per_unit", "confidence",
            "method", "rationale", "created_at",
        ]
        read_only_fields = fields


class ForecastRequestSerializer(serializers.Serializer):
    crop = serializers.PrimaryKeyRelatedField(queryset=Crop.objects.all())
    region = serializers.CharField(required=False, allow_blank=True)
    horizon_days = serializers.IntegerField(required=False, min_value=1, max_value=365, default=7)


class PriceSuggestionSerializer(serializers.Serializer):
    crop = serializers.PrimaryKeyRelatedField(queryset=Crop.objects.all())
    quality_grade = serializers.ChoiceField(choices=["A", "B", "C"], default="A")
    region = serializers.CharField(required=False, allow_blank=True)


# ---------------------------------------------------------------------------
# Negotiation (price offers)
# ---------------------------------------------------------------------------
class PriceOfferSerializer(serializers.ModelSerializer):
    listing_crop = serializers.CharField(source="listing.crop.name", read_only=True)
    farmer_name = serializers.CharField(source="listing.farmer.full_name", read_only=True)
    buyer_name = serializers.SerializerMethodField()
    listed_price = serializers.DecimalField(
        source="listing.price_per_unit", max_digits=10, decimal_places=2, read_only=True
    )
    unit_saving = serializers.DecimalField(max_digits=10, decimal_places=2, read_only=True)

    class Meta:
        model = PriceOffer
        fields = [
            "id", "listing", "listing_crop", "buyer", "buyer_name", "farmer_name",
            "quantity_requested", "proposed_price", "listed_price", "unit_saving",
            "message", "status", "response_note", "responded_at", "order",
            "created_at", "updated_at",
        ]
        read_only_fields = [
            "id", "buyer", "status", "response_note", "responded_at", "order",
            "created_at", "updated_at", "listed_price", "unit_saving",
            "listing_crop", "farmer_name", "buyer_name",
        ]

    def get_buyer_name(self, obj):
        return str(obj.buyer)

    def validate(self, attrs):
        if self.instance is None:
            request = self.context.get("request")
            buyer = getattr(request.user, "buyer_profile", None) if request else None
            if buyer is None:
                raise serializers.ValidationError("Only buyers can make an offer.")
            listing = attrs.get("listing")
            if listing and listing.farmer.user_id == request.user.id:
                raise serializers.ValidationError("You cannot offer on your own listing.")
        return attrs

    def create(self, validated_data):
        request = self.context["request"]
        try:
            return offer_service.create_offer(
                listing=validated_data["listing"],
                buyer=request.user.buyer_profile,
                quantity=validated_data["quantity_requested"],
                proposed_price=validated_data["proposed_price"],
                message=validated_data.get("message", ""),
            )
        except OfferError as exc:
            raise serializers.ValidationError(str(exc))


class PriceOfferRespondSerializer(serializers.Serializer):
    """Farmer's accept/reject decision on an offer."""

    action = serializers.ChoiceField(choices=["accept", "reject"])
    note = serializers.CharField(required=False, allow_blank=True, max_length=255)

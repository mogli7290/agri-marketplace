"""DRF views and viewsets for the marketplace API."""

import logging

from django.conf import settings
from django.contrib.auth import authenticate, get_user_model
from django.db.models import Q
from django_filters.rest_framework import DjangoFilterBackend
from rest_framework import generics, permissions, status, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework_simplejwt.tokens import RefreshToken
from rest_framework_simplejwt.views import TokenObtainPairView

from marketplace.models import (
    BuyerProfile,
    Crop,
    DeliveryPartner,
    DemandForecast,
    FarmerProfile,
    Listing,
    Order,
    Payment,
    PriceOffer,
    Shipment,
)
from marketplace.services import accounts as accounts_service
from marketplace.services import ai as ai_service
from marketplace.services import forecasting, logistics, payments, upi
from marketplace.services import offers as offer_service
from marketplace.services import orders as order_service
from marketplace.services.offers import OfferError

from . import serializers
from .permissions import IsBuyer, IsFarmer, IsOwnerOrReadOnly

User = get_user_model()


class AIScopedThrottle(ScopedRateThrottle):
    scope = "ai"


logger = logging.getLogger(__name__)


# ===========================================================================
# Auth
# ===========================================================================
class RegisterAPIView(generics.CreateAPIView):
    """Create an account.

    When email verification is on, no tokens are returned: the caller must
    confirm the address (see ``EmailVerification``) before it can sign in.
    """

    serializer_class = serializers.RegisterSerializer
    permission_classes = [permissions.AllowAny]
    authentication_classes = []

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user = serializer.save()

        if settings.EMAIL_VERIFICATION_REQUIRED and not user.is_superuser:
            accounts_service.issue_verification(
                user,
                base_url=request.build_absolute_uri("/"),
            )
            return Response(
                {
                    "user": serializers.UserSerializer(user).data,
                    "email_verified": False,
                    "detail": (
                        "Account created. Confirm the emailed link, then sign in to "
                        "receive tokens."
                    ),
                },
                status=status.HTTP_201_CREATED,
            )

        refresh = RefreshToken.for_user(user)
        return Response(
            {
                "user": serializers.UserSerializer(user).data,
                "email_verified": True,
                "refresh": str(refresh),
                "access": str(refresh.access_token),
            },
            status=status.HTTP_201_CREATED,
        )


class MeView(generics.RetrieveAPIView):
    """The authenticated user with their farmer/buyer profile attached."""

    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, *args, **kwargs):
        data = serializers.UserSerializer(request.user).data
        if hasattr(request.user, "farmer_profile"):
            data["farmer_profile"] = serializers.FarmerProfileSerializer(
                request.user.farmer_profile
            ).data
        if hasattr(request.user, "buyer_profile"):
            data["buyer_profile"] = serializers.BuyerProfileSerializer(
                request.user.buyer_profile
            ).data
        return Response(data)


class TokenObtainPairViewWithUser(TokenObtainPairView):
    """Standard JWT login that also echoes the user payload.

    Refuses to issue tokens to an account that has not confirmed its email —
    the password is not the only thing standing between an attacker and a
    farmer's orders.
    """

    def post(self, request, *args, **kwargs):
        if settings.EMAIL_VERIFICATION_REQUIRED:
            username = (request.data.get("username") or "").strip()
            user = User.objects.filter(
                Q(username__iexact=username) | Q(email__iexact=username)
            ).first()
            if user is not None and not accounts_service.may_sign_in(user):
                accounts_service.resend_verification(
                    user, base_url=request.build_absolute_uri("/")
                )
                return Response(
                    {"detail": "Confirm your email address before signing in."},
                    status=status.HTTP_403_FORBIDDEN,
                )

        response = super().post(request, *args, **kwargs)
        if response.status_code == 200:
            try:
                user = authenticate(
                    request,
                    username=request.data.get("username"),
                    password=request.data.get("password"),
                )
                if user:
                    response.data["user"] = serializers.UserSerializer(user).data
                    response.data["email_verified"] = accounts_service.is_verified(user)
            except Exception:  # pragma: no cover - never block login on enrichment
                logger.exception("Could not enrich token response")
        return response


# ===========================================================================
# Accounts
# ===========================================================================
class FarmerProfileViewSet(viewsets.ModelViewSet):
    queryset = FarmerProfile.objects.select_related("user").all()
    serializer_class = serializers.FarmerProfileSerializer
    permission_classes = [permissions.IsAuthenticatedOrReadOnly, IsOwnerOrReadOnly]
    owner_field = "user"

    def get_queryset(self):
        queryset = super().get_queryset()
        if district := self.request.query_params.get("district"):
            queryset = queryset.filter(district__iexact=district)
        if self.request.query_params.get("fpo") == "true":
            queryset = queryset.filter(is_fpo=True)
        return queryset


class BuyerProfileViewSet(viewsets.ModelViewSet):
    queryset = BuyerProfile.objects.select_related("user").all()
    serializer_class = serializers.BuyerProfileSerializer
    permission_classes = [permissions.IsAuthenticatedOrReadOnly, IsOwnerOrReadOnly]
    owner_field = "user"

    def get_queryset(self):
        queryset = super().get_queryset()
        if buyer_type := self.request.query_params.get("buyer_type"):
            queryset = queryset.filter(buyer_type=buyer_type)
        return queryset


# ===========================================================================
# Supply
# ===========================================================================
class CropViewSet(viewsets.ModelViewSet):
    queryset = Crop.objects.all()
    serializer_class = serializers.CropSerializer
    permission_classes = [permissions.IsAuthenticatedOrReadOnly]
    filter_backends = [DjangoFilterBackend]
    filterset_fields = ["unit", "category"]


class ListingViewSet(viewsets.ModelViewSet):
    serializer_class = serializers.ListingSerializer
    filter_backends = [DjangoFilterBackend]
    filterset_fields = ["crop", "quality_grade", "status", "is_organic", "farmer"]
    search_fields = ["crop__name", "farmer__full_name", "farmer__village", "description"]
    ordering_fields = ["price_per_unit", "created_at", "harvest_date"]

    def get_queryset(self):
        queryset = Listing.objects.select_related("crop", "farmer").all()
        # Anonymous/default browsing shows active listings only.
        if not self.request.query_params.get("status"):
            if self.action == "list":
                queryset = queryset.filter(status="active")
        max_price = self.request.query_params.get("max_price")
        if max_price:
            queryset = queryset.filter(price_per_unit__lte=max_price)
        return queryset

    def get_permissions(self):
        if self.action in {"create", "update", "partial_update", "destroy"}:
            return [permissions.IsAuthenticated(), IsFarmer()]
        return [permissions.IsAuthenticatedOrReadOnly()]

    def perform_create(self, serializer):
        serializer.save(farmer=self.request.user.farmer_profile)

    @action(detail=True, methods=["post"], throttle_classes=[AIScopedThrottle])
    def suggest_price(self, request, pk=None):
        """AI price suggestion for a listing (advisory, stored on the listing)."""
        listing = self.get_object()
        result = ai_service.suggest_price(
            crop_name=listing.crop.name,
            quality_grade=listing.quality_grade,
            region=listing.farmer.district,
            base_price=listing.price_per_unit,
            unit=listing.crop.unit,
        )
        if result["price"] is not None:
            listing.ai_suggested_price = result["price"]
            listing.save(update_fields=["ai_suggested_price"])
        return Response(
            {
                "ai_suggested_price": result["price"],
                "current_price": listing.price_per_unit,
                "method": result["method"],
                "rationale": result["rationale"],
                "crop": listing.crop.name,
                "quality_grade": listing.quality_grade,
            }
        )

    @action(detail=True, methods=["post"], throttle_classes=[AIScopedThrottle])
    def forecast(self, request, pk=None):
        """Demand forecast for the listing's crop."""
        listing = self.get_object()
        horizon = int(request.data.get("horizon_days", 7))
        result = forecasting.forecast_for_crop(
            listing.crop, listing.farmer.district, horizon_days=horizon
        )
        return Response(result)


# ===========================================================================
# Demand
# ===========================================================================
class OrderViewSet(viewsets.ModelViewSet):
    serializer_class = serializers.OrderSerializer
    filter_backends = [DjangoFilterBackend]
    filterset_fields = ["status", "payment_status"]
    ordering_fields = ["created_at", "updated_at"]

    def get_queryset(self):
        user = self.request.user
        queryset = Order.objects.select_related(
            "buyer__user", "listing__crop", "listing__farmer"
        ).prefetch_related("status_history", "payments")
        if user.is_staff:
            return queryset
        if hasattr(user, "farmer_profile"):
            return queryset.filter(listing__farmer=user.farmer_profile)
        if hasattr(user, "buyer_profile"):
            return queryset.filter(buyer=user.buyer_profile)
        return queryset.none()

    def get_permissions(self):
        if self.action == "create":
            return [permissions.IsAuthenticated(), IsBuyer()]
        return [permissions.IsAuthenticated()]

    @action(detail=True, methods=["post"])
    def advance_status(self, request, pk=None):
        order = self.get_object()
        new_status = request.data.get("status")
        try:
            order_service.advance_status(
                order, new_status, user=request.user, note=request.data.get("note", "")
            )
        except order_service.OrderError as exc:
            return Response({"error": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(serializers.OrderSerializer(order).data)

    @action(detail=True, methods=["post"])
    def cancel(self, request, pk=None):
        order = self.get_object()
        if hasattr(request.user, "buyer_profile") and order.buyer_id != request.user.buyer_profile.id:
            return Response({"error": "Not allowed"}, status=status.HTTP_403_FORBIDDEN)
        try:
            order_service.advance_status(
                order, "cancelled", user=request.user, note=request.data.get("reason", "")
            )
        except order_service.OrderError as exc:
            return Response({"error": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(serializers.OrderSerializer(order).data)

    @action(detail=True, methods=["post"])
    def initiate_payment(self, request, pk=None):
        order = self.get_object()
        if not (hasattr(request.user, "buyer_profile") and order.buyer_id == request.user.buyer_profile.id):
            return Response({"error": "Only the buyer can pay"}, status=status.HTTP_403_FORBIDDEN)
        try:
            payment = payments.start_payment(order)
        except payments.PaymentError as exc:
            return Response({"error": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(
            {
                "payment_id": payment.id,
                "provider_order_id": payment.provider_order_id,
                "amount": payments.to_paise(payment.amount),
                "currency": payment.currency,
            }
        )

    @action(detail=True, methods=["post"])
    def verify_payment(self, request, pk=None):
        order = self.get_object()
        try:
            payments.record_capture(
                order,
                provider_order_id=request.data.get("razorpay_order_id", ""),
                payment_id=request.data.get("razorpay_payment_id", ""),
                signature=request.data.get("razorpay_signature", ""),
            )
        except payments.PaymentError as exc:
            return Response({"error": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        return Response({"status": "paid", "order_id": order.pk})

    @action(detail=True, methods=["post"])
    def upi_paid(self, request, pk=None):
        """Record a direct-UPI payment claim (UTR) for an order.

        The order is not settled yet — staff confirm the credit in the admin
        before it moves to paid.
        """
        order = self.get_object()
        if not (hasattr(request.user, "buyer_profile") and order.buyer_id == request.user.buyer_profile.id):
            return Response({"error": "Only the buyer can record a payment"}, status=status.HTTP_403_FORBIDDEN)
        if order.payment_status == "paid":
            return Response({"error": "Already paid"}, status=status.HTTP_400_BAD_REQUEST)

        utr = request.data.get("utr") or ""
        try:
            payment = upi.record_claim(order, utr)
        except upi.UpiError as exc:
            return Response({"error": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        return Response(
            {
                "status": "authorized",
                "payment_id": payment.id,
                "note": "Recorded. An operator will confirm the UPI credit before the order is marked paid.",
            },
            status=status.HTTP_201_CREATED,
        )


class PaymentViewSet(viewsets.ReadOnlyModelViewSet):
    serializer_class = serializers.PaymentSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        user = self.request.user
        queryset = Payment.objects.select_related("order").all()
        if user.is_staff:
            return queryset
        filters = Q()
        if hasattr(user, "buyer_profile"):
            filters |= Q(order__buyer=user.buyer_profile)
        if hasattr(user, "farmer_profile"):
            filters |= Q(order__listing__farmer=user.farmer_profile)
        return queryset.filter(filters) if filters else queryset.none()


# ===========================================================================
# Logistics
# ===========================================================================
class DeliveryPartnerViewSet(viewsets.ModelViewSet):
    queryset = DeliveryPartner.objects.all()
    serializer_class = serializers.DeliveryPartnerSerializer
    permission_classes = [permissions.IsAuthenticatedOrReadOnly]

    def get_permissions(self):
        if self.action in {"create", "update", "partial_update", "destroy"}:
            return [permissions.IsAdminUser()]
        return [permissions.IsAuthenticatedOrReadOnly()]


class ShipmentViewSet(viewsets.ReadOnlyModelViewSet):
    serializer_class = serializers.ShipmentSerializer
    permission_classes = [permissions.IsAuthenticated]
    filter_backends = [DjangoFilterBackend]
    filterset_fields = ["status", "partner"]

    def get_queryset(self):
        user = self.request.user
        queryset = Shipment.objects.select_related("partner").prefetch_related("stops")
        if user.is_staff:
            return queryset
        if hasattr(user, "delivery_partner"):
            return queryset.filter(partner=user.delivery_partner)
        if hasattr(user, "farmer_profile"):
            return queryset.filter(stops__order__listing__farmer=user.farmer_profile).distinct()
        if hasattr(user, "buyer_profile"):
            return queryset.filter(stops__order__buyer=user.buyer_profile).distinct()
        return queryset.none()

    @action(detail=False, methods=["post"], permission_classes=[permissions.IsAdminUser])
    def plan(self, request):
        """Plan an optimised shipment for a set of orders (staff only)."""
        serializer = serializers.PlanShipmentSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        partner = None
        partner_id = serializer.validated_data.get("partner_id")
        if partner_id:
            partner = DeliveryPartner.objects.filter(pk=partner_id).first()
        try:
            shipment = logistics.plan_shipment(
                serializer.validated_data["order_ids"], partner=partner
            )
        except logistics.LogisticsError as exc:
            return Response({"error": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(
            serializers.ShipmentSerializer(shipment).data, status=status.HTTP_201_CREATED
        )


# ===========================================================================
# Intelligence
# ===========================================================================
class DemandForecastViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = DemandForecast.objects.select_related("crop").all()
    serializer_class = serializers.DemandForecastSerializer
    permission_classes = [permissions.IsAuthenticatedOrReadOnly]
    filter_backends = [DjangoFilterBackend]
    filterset_fields = ["crop", "region", "method"]

    @action(detail=False, methods=["post"], throttle_classes=[AIScopedThrottle])
    def generate(self, request):
        """Generate (and persist) a fresh forecast for a crop/region."""
        serializer = serializers.ForecastRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        result = forecasting.forecast_for_crop(
            data["crop"], data.get("region", ""), horizon_days=data["horizon_days"]
        )
        return Response(result, status=status.HTTP_201_CREATED)


# ===========================================================================
# Negotiation (price offers)
# ===========================================================================
class PriceOfferViewSet(viewsets.ModelViewSet):
    """Buyers create offers; farmers accept/reject them.

    Accepting an offer creates an :class:`Order` at the agreed price.
    """
    serializer_class = serializers.PriceOfferSerializer
    permission_classes = [permissions.IsAuthenticated]
    filter_backends = [DjangoFilterBackend]
    filterset_fields = ["status", "listing"]

    def get_queryset(self):
        user = self.request.user
        queryset = PriceOffer.objects.select_related(
            "listing__crop", "listing__farmer", "buyer__user", "order"
        )
        if user.is_staff:
            return queryset
        if hasattr(user, "farmer_profile"):
            return queryset.filter(listing__farmer=user.farmer_profile)
        if hasattr(user, "buyer_profile"):
            return queryset.filter(buyer=user.buyer_profile)
        return queryset.none()

    def get_permissions(self):
        if self.action == "create":
            return [permissions.IsAuthenticated(), IsBuyer()]
        return [permissions.IsAuthenticated()]

    def perform_create(self, serializer):
        # The service re-validates (stock, own listing, duplicate pending).
        serializer.save()

    @action(detail=True, methods=["post"])
    def respond(self, request, pk=None):
        """Farmer accepts or rejects the offer; accept also creates the order."""
        serializer = serializers.PriceOfferRespondSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            offer = offer_service.respond_offer(
                self.get_object(),
                serializer.validated_data["action"],
                request.user,
                note=serializer.validated_data.get("note", ""),
            )
        except OfferError as exc:
            return Response({"error": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(self.get_serializer(offer).data)

    @action(detail=True, methods=["post"])
    def cancel(self, request, pk=None):
        """Buyer withdraws a pending offer."""
        try:
            offer = offer_service.cancel_offer(self.get_object(), request.user)
        except OfferError as exc:
            return Response({"error": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(self.get_serializer(offer).data)

"""Marketplace API URL configuration."""

from django.urls import include, path
from rest_framework.routers import DefaultRouter
from rest_framework_simplejwt.views import TokenRefreshView

from marketplace.api.views import (
    BuyerProfileViewSet,
    CropViewSet,
    DeliveryPartnerViewSet,
    DemandForecastViewSet,
    FarmerProfileViewSet,
    ListingViewSet,
    MeView,
    OrderViewSet,
    PaymentViewSet,
    PriceOfferViewSet,
    RegisterAPIView,
    ShipmentViewSet,
    TokenObtainPairViewWithUser,
)

router = DefaultRouter()
router.register(r"crops", CropViewSet, basename="crop")
router.register(r"listings", ListingViewSet, basename="listing")
router.register(r"orders", OrderViewSet, basename="order")
router.register(r"payments", PaymentViewSet, basename="payment")
router.register(r"farmers", FarmerProfileViewSet, basename="farmer")
router.register(r"buyers", BuyerProfileViewSet, basename="buyer")
router.register(r"partners", DeliveryPartnerViewSet, basename="partner")
router.register(r"shipments", ShipmentViewSet, basename="shipment")
router.register(r"forecasts", DemandForecastViewSet, basename="forecast")
router.register(r"offers", PriceOfferViewSet, basename="offer")

urlpatterns = [
    path("auth/register/", RegisterAPIView.as_view(), name="api-register"),
    path("auth/token/", TokenObtainPairViewWithUser.as_view(), name="api-token"),
    path("auth/token/refresh/", TokenRefreshView.as_view(), name="api-token-refresh"),
    path("auth/me/", MeView.as_view(), name="api-me"),
    path("", include(router.urls)),
]

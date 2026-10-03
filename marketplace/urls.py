"""Marketplace app URL configuration."""

from django.urls import path

from . import views

app_name = "marketplace"

urlpatterns = [
    # Home / listing browsing
    path("", views.ListingListView.as_view(), name="listing_list"),
    path("listings/create/", views.ListingCreateView.as_view(), name="listing_create"),
    path("listings/price-hint/", views.ListingPriceHintView.as_view(), name="listing_price_hint"),
    path("listings/<int:pk>/", views.ListingDetailView.as_view(), name="listing_detail"),
    path(
        "listings/<int:pk>/contact/",
        views.ContactSellerView.as_view(),
        name="contact_seller",
    ),
    path("listings/<int:pk>/edit/", views.ListingUpdateView.as_view(), name="listing_update"),
    path("listings/<int:pk>/cancel/", views.ListingCancelView.as_view(), name="listing_cancel"),
    path("listings/<int:pk>/order/", views.OrderCreateView.as_view(), name="order_create"),

    # Orders
    path("orders/<int:pk>/", views.OrderDetailView.as_view(), name="order_detail"),
    path("orders/<int:pk>/cancel/", views.OrderCancelView.as_view(), name="order_cancel"),
    path("orders/<int:pk>/pay/", views.PaymentInitiateView.as_view(), name="payment_initiate"),
    path("orders/<int:pk>/upi-paid/", views.UpiPaidView.as_view(), name="upi_paid"),
    path(
        "orders/<int:pk>/direct-paid/",
        views.SellerPaymentRecordView.as_view(),
        name="direct_payment_record",
    ),
    path(
        "payments/<int:pk>/confirm-receipt/",
        views.SellerPaymentConfirmView.as_view(),
        name="direct_payment_confirm",
    ),
    # Proof of delivery
    path(
        "orders/<int:pk>/delivery-code/",
        views.DeliveryCodeView.as_view(),
        name="delivery_code",
    ),
    path(
        "orders/<int:pk>/delivery-proof/",
        views.DeliveryProofView.as_view(),
        name="delivery_proof",
    ),
    path(
        "orders/<int:pk>/delivery-response/",
        views.DeliveryConfirmView.as_view(),
        name="delivery_response",
    ),
    path(
        "orders/<int:pk>/dispute/<str:action>/",
        views.DeliveryDisputeResolveView.as_view(),
        name="delivery_dispute_resolve",
    ),

    path("orders/<int:pk>/<str:action>/", views.OrderStatusUpdateView.as_view(), name="order_status"),

    # Payments
    path("payments/verify/", views.PaymentVerifyView.as_view(), name="payment_verify"),
    path("payments/webhook/", views.PaymentWebhookView.as_view(), name="payment_webhook"),
    path("payments/reconcile/", views.ReconciliationView.as_view(), name="payment_reconcile"),
    path(
        "payments/reconcile/<int:pk>/",
        views.ReconciliationPreviewView.as_view(),
        name="payment_reconcile_preview",
    ),

    # Demand board (buyers post needs, sellers respond)
    path("requests/new/", views.DemandRequestCreateView.as_view(), name="request_create"),
    path("requests/", views.DemandRequestListView.as_view(), name="request_list"),
    path("requests/mine/", views.MyRequestsView.as_view(), name="my_requests"),
    path("requests/<int:pk>/", views.RequestDetailView.as_view(), name="request_detail"),
    path("requests/<int:pk>/close/", views.RequestCloseView.as_view(), name="request_close"),
    path(
        "requests/<int:pk>/offer/",
        views.RequestOfferCreateView.as_view(),
        name="request_offer_create",
    ),
    path(
        "request-offers/<int:pk>/<str:action>/",
        views.RequestOfferActionView.as_view(),
        name="request_offer_action",
    ),
    path("messages/", views.ConversationListView.as_view(), name="conversations"),
    path("messages/<int:pk>/", views.ConversationView.as_view(), name="conversation"),
    path("payout-methods/", views.PayoutMethodView.as_view(), name="payout_methods"),
    path("earnings/", views.EarningsView.as_view(), name="earnings"),
    path(
        "partner/payout-methods/",
        views.PartnerPayoutMethodView.as_view(),
        name="partner_payout_methods",
    ),
    path("partner/earnings/", views.PartnerEarningsView.as_view(), name="partner_earnings"),
    path(
        "staff/payouts/<int:pk>/<str:action>/",
        views.PayoutActionView.as_view(),
        name="payout_action",
    ),

    # Price offers (negotiation)
    path("offers/", views.OffersView.as_view(), name="offers"),
    path("offers/create/<int:pk>/", views.OfferCreateView.as_view(), name="offer_create"),
    path("offers/<int:pk>/<str:action>/", views.OfferActionView.as_view(), name="offer_action"),

    # Logistics
    path("shipments/", views.ShipmentListView.as_view(), name="shipment_list"),
    path("shipments/plan/", views.PlanShipmentView.as_view(), name="plan_shipment"),
    path("shipments/<int:pk>/", views.ShipmentDetailView.as_view(), name="shipment_detail"),
    path(
        "shipments/<int:pk>/complete/",
        views.ShipmentCompleteView.as_view(),
        name="shipment_complete",
    ),


    # Forecasting
    path("forecast/", views.ForecastView.as_view(), name="forecast"),
    path("forecast/request/", views.ForecastRequestView.as_view(), name="forecast_request"),

    # Dashboards
    path("dashboard/", views.DashboardView.as_view(), name="dashboard"),
    path("dashboard/farmer/", views.FarmerDashboardView.as_view(), name="farmer_dashboard"),
    path("dashboard/buyer/", views.BuyerDashboardView.as_view(), name="buyer_dashboard"),

    # Auth
    path("login/", views.LoginView.as_view(), name="login"),
    path("logout/", views.LogoutView.as_view(), name="logout"),
    path("register/", views.RegisterView.as_view(), name="register"),
    path(
        "password-reset/",
        views.PasswordResetRequestView.as_view(),
        name="password_reset_request",
    ),
    path(
        "password-reset/done/",
        views.PasswordResetDoneView.as_view(),
        name="password_reset_done",
    ),
    path(
        "password-reset/<str:uidb64>/<str:token>/",
        views.PasswordResetConfirmView.as_view(),
        name="password_reset_confirm",
    ),
    path(
        "verify-email/resend/",
        views.VerificationResendView.as_view(),
        name="verification_resend",
    ),
    path(
        "verify-email/sent/",
        views.VerificationSentView.as_view(),
        name="verification_sent",
    ),
    path("verify-email/<str:token>/", views.EmailVerificationView.as_view(), name="verify_email"),
    path("profile/", views.ProfileView.as_view(), name="profile"),
    path("profile/edit/", views.ProfileUpdateView.as_view(), name="profile_edit"),
]

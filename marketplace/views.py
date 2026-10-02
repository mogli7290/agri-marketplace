"""Web views for browsing, listings, orders, dashboards, payments and logistics."""

import json
import logging

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import get_user_model, login as auth_login, logout as auth_logout
from django.contrib.auth.mixins import LoginRequiredMixin, UserPassesTestMixin
from django.db.models import F, Q, Sum
from django.http import HttpResponseBadRequest, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse, reverse_lazy
from django.contrib.auth.tokens import default_token_generator
from django.utils.decorators import method_decorator
from django.views import View
from django.views.decorators.csrf import csrf_exempt
from django.views.generic import CreateView, DetailView, FormView, ListView, TemplateView, UpdateView

from marketplace import forms
from marketplace.models import (
    BuyerProfile,
    Conversation,
    Crop,
    DemandForecast,
    DemandRequest,
    DeliveryProof,
    FarmerProfile,
    Listing,
    Order,
    Payment,
    Payout,
    PriceOffer,
    Reconciliation,
    RequestOffer,
    Shipment,
)
from marketplace.services import accounts as accounts_service
from marketplace.services import deals as deals_service
from marketplace.services import delivery as delivery_service
from marketplace.services import payouts as payout_service
from marketplace.services import ai as ai_service
from marketplace.services import forecasting, logistics, payments, reconciliation as reconciliation_service, upi
from marketplace.services import offers as offer_service
from marketplace.services import orders as order_service

logger = logging.getLogger(__name__)
User = get_user_model()


# ===========================================================================
# Authentication
# ===========================================================================
class LoginView(FormView):
    template_name = "auth/login.html"
    form_class = forms.LoginForm
    success_url = reverse_lazy("marketplace:dashboard")

    def get(self, request, *args, **kwargs):
        if request.user.is_authenticated:
            return redirect("marketplace:dashboard")
        return super().get(request, *args, **kwargs)

    def get_success_url(self):
        return self.request.GET.get("next") or super().get_success_url()

    def form_valid(self, form):
        user = form.get_user()
        # Password is correct, but the account has not proved its address yet.
        if not accounts_service.may_sign_in(user):
            if accounts_service.resend_verification(
                user, base_url=accounts_service.site_base_url(self.request)
            ):
                messages.warning(
                    self.request,
                    "Your email address is not confirmed yet — we sent you a new link.",
                )
            else:
                messages.warning(
                    self.request,
                    "Your email address is not confirmed yet. Check your inbox, or "
                    "request a new link from the sign-in page.",
                )
            return redirect(f"{reverse('marketplace:login')}?next={self.request.GET.get('next', '')}")

        auth_login(self.request, user)
        messages.success(self.request, "Welcome back!")
        return super().form_valid(form)


class LogoutView(View):
    """Logout must be a POST to avoid CSRF-triggered logouts."""

    def post(self, request):
        auth_logout(request)
        messages.success(request, "You have been logged out.")
        return redirect("marketplace:login")

    def get(self, request):
        return redirect("marketplace:dashboard")


class RegisterView(CreateView):
    template_name = "auth/register.html"
    form_class = forms.RegistrationForm
    success_url = reverse_lazy("marketplace:dashboard")

    def get(self, request, *args, **kwargs):
        if request.user.is_authenticated:
            return redirect("marketplace:dashboard")
        return super().get(request, *args, **kwargs)

    def form_valid(self, form):
        user = form.save()
        data = form.cleaned_data
        if data["user_type"] == "farmer":
            FarmerProfile.objects.create(
                user=user,
                full_name=data["full_name"],
                phone_number=data["phone_number"],
                village=data["village"],
                district=data["district"],
                state=data["state"],
                is_fpo=data.get("is_fpo", False),
            )
        else:
            BuyerProfile.objects.create(
                user=user,
                buyer_type=data["buyer_type"],
                business_name=data.get("business_name", ""),
                phone_number=data["phone_number"],
                city=data["city"],
                address=data.get("address", ""),
            )

        # Prove the address before the account can be used.
        if settings.EMAIL_VERIFICATION_REQUIRED:
            accounts_service.issue_verification(
                user, base_url=accounts_service.site_base_url(self.request)
            )
            return redirect(
                f"{reverse('marketplace:verification_sent')}?email={user.email}"
            )

        auth_login(self.request, user, backend="marketplace.auth_backends.EmailOrUsernameBackend")
        messages.success(self.request, "Your account is ready. Welcome to AgriMarket!")
        return redirect("marketplace:dashboard")


class EmailVerificationView(View):
    """Landing page for the link in the confirmation email."""

    template_name = "auth/verify_email_result.html"

    def get(self, request, token):
        try:
            record = accounts_service.confirm_token(token)
        except accounts_service.VerificationError as exc:
            return render(request, self.template_name, {"verified": False, "error": str(exc)})

        logger.info("Email verified for %s", record.user)
        return render(request, self.template_name, {"verified": True})


class VerificationSentView(TemplateView):
    """"Check your inbox" page."""

    template_name = "auth/verify_email_sent.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["email"] = self.request.GET.get("email", "")
        context["expiry_hours"] = max(1, settings.EMAIL_VERIFICATION_MAX_AGE // 3600)
        return context


class VerificationResendView(FormView):
    """Request a fresh confirmation link.

    Always reports the same thing, whether or not the address exists, so the page
    cannot be used to enumerate who has an account.
    """

    template_name = "auth/verify_email_resend.html"
    form_class = forms.EmailResendForm
    success_url = reverse_lazy("marketplace:verification_sent")

    def form_valid(self, form):
        email = form.cleaned_data["email"]
        user = User.objects.filter(email__iexact=email).first()
        if user is not None:
            accounts_service.resend_verification(
                user, base_url=accounts_service.site_base_url(self.request)
            )
        return redirect(f"{reverse('marketplace:verification_sent')}")


class PasswordResetRequestView(FormView):
    """Step one of "forgot my password": email a reset link.

    Always reports the same thing whether or not the address exists, so this
    page cannot be used to find out who has an account here. Overriding
    ``form_valid`` (rather than using Django's ``PasswordResetView``) is what
    makes that possible.
    """

    template_name = "auth/password_reset_form.html"
    form_class = forms.PasswordResetForm
    success_url = reverse_lazy("marketplace:password_reset_done")

    def get(self, request, *args, **kwargs):
        if request.user.is_authenticated:
            return redirect("marketplace:dashboard")
        return super().get(request, *args, **kwargs)

    def form_valid(self, form):
        user = User.objects.filter(email__iexact=form.cleaned_data["email"]).first()
        if user is not None and user.has_usable_password():
            delivered = accounts_service.send_password_reset_email(
                user,
                default_token_generator.make_token(user),
                base_url=accounts_service.site_base_url(self.request),
            )
            if not delivered:
                # Honest failure beats a silent one: the user needs to know the
                # link never left the building.
                messages.warning(
                    self.request,
                    "We could not send the email just now. Please try again in a "
                    "few minutes.",
                )
        return super().form_valid(form)


class PasswordResetDoneView(TemplateView):
    template_name = "auth/password_reset_done.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["expiry_hours"] = max(1, settings.PASSWORD_RESET_TIMEOUT // 3600)
        return context


def _user_from_uidb64(uidb64: str):
    """Django's uidb64 is urlsafe-base64 over the primary key. Never raises."""
    from django.utils.encoding import force_bytes, force_str
    from django.utils.http import urlsafe_base64_decode

    try:
        pk = force_str(urlsafe_base64_decode(uidb64))
        return User.objects.filter(pk=pk, is_active=True).first()
    except (TypeError, ValueError, OverflowError, UnicodeDecodeError, Exception):
        return None


class PasswordResetConfirmView(FormView):
    """Step two: actually choose a new password."""

    template_name = "auth/password_reset_confirm.html"
    form_class = forms.SetPasswordForm
    success_url = reverse_lazy("marketplace:login")

    #: Set by ``get``/``post`` from the token in the URL.
    _user = None
    _token = None

    def get_user(self):
        """Decode the uidb64 segment and load the account.

        A malformed or hostile uid must produce the same "expired" page as an
        expired one, never a 500 — so the decode is wrapped.
        """
        if self._user is None:
            self._user = _user_from_uidb64(self.kwargs["uidb64"])
        return self._user

    def valid_link(self) -> bool:
        user = self.get_user()
        if user is None:
            return False
        return default_token_generator.check_token(user, self.kwargs["token"])

    def get(self, request, *args, **kwargs):
        if not self.valid_link():
            self.kwargs["invalid"] = True
        return super().get(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["valid"] = self.valid_link()
        return context

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["user"] = self.get_user()
        return kwargs

    def form_valid(self, form):
        form.save()
        messages.success(self.request, "Password changed. You can sign in now.")
        return super().form_valid(form)

    def form_invalid(self, form):
        if not self.valid_link():
            self.kwargs["invalid"] = True
            return redirect(f"{reverse('marketplace:password_reset_request')}")
        return super().form_invalid(form)

    def dispatch(self, request, *args, **kwargs):
        # An expired or forged link must never show a password form.
        if not self.valid_link():
            return render(
                request,
                "auth/password_reset_confirm.html",
                {"valid": False, "form": forms.SetPasswordForm(user=None)},
            )
        return super().dispatch(request, *args, **kwargs)


class ProfileView(LoginRequiredMixin, TemplateView):
    template_name = "auth/profile.html"


class ProfileUpdateView(LoginRequiredMixin, View):
    """Edit the farmer or buyer profile depending on the user's role."""

    def get_form(self, request):
        if hasattr(request.user, "farmer_profile"):
            return forms.FarmerProfileForm(instance=request.user.farmer_profile), "auth/profile_form.html"
        if hasattr(request.user, "buyer_profile"):
            return forms.BuyerProfileForm(instance=request.user.buyer_profile), "auth/profile_form.html"
        return None, None

    def get(self, request):
        form, template = self.get_form(request)
        if form is None:
            messages.error(request, "No profile is linked to this account.")
            return redirect("marketplace:dashboard")
        return self._render(request, form, template)

    def post(self, request):
        form, template = self.get_form(request)
        if form is None:
            return redirect("marketplace:dashboard")
        form = type(form)(request.POST, request.FILES, instance=form.instance)
        if form.is_valid():
            form.save()
            messages.success(request, "Profile updated.")
            return redirect("marketplace:profile")
        return self._render(request, form, template)

    def _render(self, request, form, template):
        return render(request, template, {"form": form})


# ===========================================================================
# Dashboards
# ===========================================================================
class DashboardView(TemplateView):
    template_name = "dashboard/main.html"

    def get(self, request, *args, **kwargs):
        if not request.user.is_authenticated:
            return redirect("marketplace:login")
        if hasattr(request.user, "farmer_profile"):
            return redirect("marketplace:farmer_dashboard")
        if hasattr(request.user, "buyer_profile"):
            return redirect("marketplace:buyer_dashboard")
        if hasattr(request.user, "delivery_partner"):
            return redirect("marketplace:shipment_list")
        return super().get(request, *args, **kwargs)


class FarmerDashboardView(LoginRequiredMixin, TemplateView):
    template_name = "dashboard/farmer/index.html"

    def get(self, request, *args, **kwargs):
        if not hasattr(request.user, "farmer_profile"):
            messages.error(request, "You need a farmer account to view that page.")
            return redirect("marketplace:dashboard")
        return super().get(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        farmer = self.request.user.farmer_profile
        listings = Listing.objects.filter(farmer=farmer).select_related("crop")
        orders = Order.objects.filter(listing__farmer=farmer).select_related(
            "buyer", "listing__crop"
        )
        revenue = orders.filter(payment_status="paid").aggregate(
            total=Sum(F("quantity_ordered") * F("agreed_price_per_unit"))
        )["total"]
        context.update(
            {
                "farmer": farmer,
                "listings": listings,
                "orders": orders,
                "active_listings": listings.filter(status="active").count(),
                "total_orders": orders.count(),
                "revenue": revenue or 0,
            }
        )
        return context


class BuyerDashboardView(LoginRequiredMixin, TemplateView):
    template_name = "dashboard/buyer/index.html"

    def get(self, request, *args, **kwargs):
        if not hasattr(request.user, "buyer_profile"):
            messages.error(request, "You need a buyer account to view that page.")
            return redirect("marketplace:dashboard")
        return super().get(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        buyer = self.request.user.buyer_profile
        orders = Order.objects.filter(buyer=buyer).select_related(
            "listing__crop", "listing__farmer"
        )
        spend = orders.filter(payment_status="paid").aggregate(
            total=Sum(F("quantity_ordered") * F("agreed_price_per_unit") + F("platform_fee") + F("delivery_fee"))
        )["total"]
        context.update(
            {
                "buyer": buyer,
                "orders": orders,
                "total_orders": orders.count(),
                "pending_payment": orders.filter(payment_status="pending").count(),
                "spend": spend or 0,
            }
        )
        return context


# ===========================================================================
# Listings
# ===========================================================================
class ListingListView(ListView):
    model = Listing
    template_name = "dashboard/listing_list.html"
    context_object_name = "listings"
    paginate_by = 12

    def get_queryset(self):
        queryset = (
            Listing.objects.filter(status="active")
            .select_related("crop", "farmer")
            .order_by("-created_at")
        )
        params = self.request.GET
        if crop := params.get("crop"):
            queryset = queryset.filter(crop_id=crop)
        if district := params.get("district"):
            queryset = queryset.filter(farmer__district__icontains=district)
        if q := params.get("q"):
            queryset = queryset.filter(
                Q(crop__name__icontains=q)
                | Q(farmer__full_name__icontains=q)
                | Q(farmer__village__icontains=q)
            )
        if organic := params.get("organic"):
            queryset = queryset.filter(is_organic=True)
        if price_min := params.get("price_min"):
            queryset = queryset.filter(price_per_unit__gte=price_min)
        if price_max := params.get("price_max"):
            queryset = queryset.filter(price_per_unit__lte=price_max)
        return queryset

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["crops"] = Crop.objects.all()
        context["filters"] = self.request.GET
        return context


class ListingDetailView(DetailView):
    model = Listing
    template_name = "dashboard/listing_detail.html"
    context_object_name = "listing"

    def get_queryset(self):
        return Listing.objects.select_related("crop", "farmer").prefetch_related("orders")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        user = self.request.user
        buyer = getattr(user, "buyer_profile", None)
        is_owner = self.object.farmer.user_id == user.id

        can_buy = bool(buyer and self.object.is_orderable and not is_owner)
        context["can_place_order"] = can_buy
        context["is_owner"] = is_owner
        context["order_form"] = forms.OrderForm(listing=self.object)
        context["offer_form"] = forms.OfferForm(listing=self.object) if can_buy else None
        context["my_offer"] = (
            self.object.offers.filter(buyer=buyer, status="pending").first()
            if can_buy
            else None
        )
        context["incoming_offers"] = (
            self.object.offers.select_related("buyer__user").order_by("-created_at")[:10]
            if is_owner
            else PriceOffer.objects.none()
        )
        context["related"] = (
            Listing.objects.filter(crop=self.object.crop, status="active")
            .exclude(pk=self.object.pk)
            .select_related("farmer")[:4]
        )
        return context


class ListingCreateView(LoginRequiredMixin, UserPassesTestMixin, CreateView):
    model = Listing
    form_class = forms.ListingForm
    template_name = "dashboard/listing_form.html"
    success_url = reverse_lazy("marketplace:farmer_dashboard")

    def test_func(self):
        return hasattr(self.request.user, "farmer_profile")

    def form_valid(self, form):
        form.instance.farmer = self.request.user.farmer_profile
        messages.success(self.request, "Listing published.")
        return super().form_valid(form)


class ListingUpdateView(LoginRequiredMixin, UserPassesTestMixin, UpdateView):
    model = Listing
    form_class = forms.ListingForm
    template_name = "dashboard/listing_form.html"

    def test_func(self):
        listing = self.get_object()
        return listing.farmer.user_id == self.request.user.id and listing.status == "active"

    def get_success_url(self):
        messages.success(self.request, "Listing updated.")
        return reverse("marketplace:listing_detail", kwargs={"pk": self.object.pk})


class ListingCancelView(LoginRequiredMixin, UserPassesTestMixin, View):
    def test_func(self):
        listing = get_object_or_404(Listing, pk=self.kwargs["pk"])
        return listing.farmer.user_id == self.request.user.id

    def post(self, request, pk):
        listing = get_object_or_404(Listing, pk=pk)
        listing.status = "cancelled"
        listing.save(update_fields=["status"])
        messages.success(request, "Listing cancelled.")
        return redirect("marketplace:farmer_dashboard")


class ListingPriceHintView(LoginRequiredMixin, View):
    """AJAX endpoint returning an AI price suggestion for a crop/grade."""

    def post(self, request):
        crop = get_object_or_404(Crop, pk=request.POST.get("crop"))
        grade = request.POST.get("quality_grade", "A")
        region = getattr(getattr(request.user, "farmer_profile", None), "district", "") or ""
        result = ai_service.suggest_price(crop.name, grade, region, base_price=None, unit=crop.unit)
        return JsonResponse(
            {
                "price": str(result["price"]) if result["price"] is not None else None,
                "method": result["method"],
                "rationale": result["rationale"],
            }
        )


# ===========================================================================
# Orders
# ===========================================================================
class OrderCreateView(LoginRequiredMixin, View):
    def post(self, request, pk):
        listing = get_object_or_404(Listing.objects.select_related("crop", "farmer"), pk=pk)
        if not hasattr(request.user, "buyer_profile"):
            messages.error(request, "Only buyers can place orders.")
            return redirect("marketplace:listing_detail", pk=pk)
        if listing.farmer.user_id == request.user.id:
            messages.error(request, "You cannot order your own listing.")
            return redirect("marketplace:listing_detail", pk=pk)

        form = forms.OrderForm(request.POST, listing=listing)
        if not form.is_valid():
            for error in form.errors.values():
                messages.error(request, error.as_text())
            return redirect("marketplace:listing_detail", pk=pk)

        try:
            order = order_service.create_order(
                buyer=request.user.buyer_profile,
                listing=listing,
                quantity=form.cleaned_data["quantity_ordered"],
            )
        except order_service.OrderError as exc:
            messages.error(request, str(exc))
            return redirect("marketplace:listing_detail", pk=pk)

        messages.success(request, f"Order #{order.pk} placed.")
        return redirect("marketplace:order_detail", pk=order.pk)


class OrderDetailView(LoginRequiredMixin, DetailView):
    model = Order
    template_name = "dashboard/order_detail.html"
    context_object_name = "order"

    def get_queryset(self):
        user = self.request.user
        queryset = Order.objects.select_related(
            "buyer__user", "listing__crop", "listing__farmer"
        ).prefetch_related("status_history", "payments")
        if hasattr(user, "farmer_profile"):
            return queryset.filter(listing__farmer=user.farmer_profile)
        if hasattr(user, "buyer_profile"):
            return queryset.filter(buyer=user.buyer_profile)
        # The assigned driver needs to see the order they just delivered —
        # otherwise they record a proof against a page they cannot open.
        partner = getattr(user, "delivery_partner", None)
        if partner is not None:
            return queryset.filter(
                shipment_stops__shipment__partner=partner
            ).distinct()
        return queryset.none()

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["payment_configured"] = payments.is_configured()
        context["upi_configured"] = upi.is_configured()
        context["upi_id"] = settings.UPI_ID if upi.is_configured() else ""
        context["upi_link"] = upi.build_upi_link(self.object) if upi.is_configured() else ""
        context["upi_payee"] = upi.payee_name() if upi.is_configured() else ""
        context["upi_claimed"] = any(
            p.provider == "upi_direct" and p.status == "authorized" for p in self.object.payments.all()
        )
        # A cancelled or already-settled order must not show a pay button.
        context["can_pay"] = (
            self.object.payment_status not in {"paid", "refunded"}
            and self.object.status in order_service.PAYABLE_STATUSES
        )
        # Direct route: pay the farmer's own UPI ID. No platform fee on these.
        context["is_direct"] = self.object.payment_route == "direct"
        context["farmer_upi_id"] = ""
        context["farmer_upi_link"] = ""
        context["farmer_paid_claim"] = any(
            p.provider == upi.FARMER_PROVIDER and p.status == "authorized"
            for p in self.object.payments.all()
        )
        if self.object.payment_route == "direct":
            method = deals_service.payout_method_for(self.object.listing.farmer)
            if method is not None and method.kind == "upi" and method.upi_id:
                context["farmer_upi_id"] = method.upi_id
                context["farmer_upi_link"] = upi.build_seller_upi_link(
                    self.object, method.upi_id, self.object.listing.farmer.full_name
                )
        context["actions"] = self.object.STATUS_TRANSITIONS.get(self.object.status, [])
        context["contact"] = deals_service.order_contact(self.object, self.request.user)
        self._add_delivery_context(context)
        return context

    def _add_delivery_context(self, context):
        """Everything the order page needs to show about proof of delivery."""
        user = self.request.user
        proof = DeliveryProof.objects.filter(order=self.object).order_by("-created_at").first()
        is_buyer = (
            getattr(user, "buyer_profile", None) is not None
            and user.buyer_profile.id == self.object.buyer_id
        )
        is_partner = bool(
            getattr(user, "delivery_partner", None)
            and self.object.shipment_stops.filter(
                shipment__partner=user.delivery_partner
            ).exists()
        )
        has_stop = self.object.shipment_stops.filter(kind="delivery").exists()

        context.update(
            {
                "proof": proof,
                "evidence": delivery_service.proof_summary(proof),
                "has_delivery_stop": has_stop,
                "is_buyer_of_order": is_buyer,
                "can_record_delivery": (is_partner or user.is_staff) and has_stop,
                "can_request_code": (is_buyer or user.is_staff) and has_stop,
                "can_respond_delivery": (is_buyer or user.is_staff)
                and proof is not None
                and proof.status != "confirmed",
                "can_resolve_dispute": user.is_staff and bool(proof and proof.is_disputed),
                "proof_form": forms.DeliveryProofForm(),
                "dispute_form": forms.DeliveryDisputeForm(),
                # Shown straight after requesting, so a farmer without data can
                # still read the code back to the driver.
                "delivery_code": self.request.session.pop("delivery_code", ""),
            }
        )


class OrderStatusUpdateView(LoginRequiredMixin, View):
    """Advance an order through its lifecycle. Participants only."""

    ACTION_TO_STATUS = {
        "confirm": "confirmed",
        "pickup": "picked_up",
        "dispatch": "in_transit",
        "deliver": "delivered",
    }

    def post(self, request, pk, action):
        order = get_object_or_404(Order.objects.select_related("listing__farmer", "buyer"), pk=pk)
        is_participant = (
            order.buyer.user_id == request.user.id
            or order.listing.farmer.user_id == request.user.id
            or request.user.is_staff
        )
        if not is_participant:
            messages.error(request, "You are not allowed to update this order.")
            return redirect("marketplace:order_detail", pk=pk)

        new_status = self.ACTION_TO_STATUS.get(action)
        if new_status is None:
            return HttpResponseBadRequest("Unknown action")

        try:
            order_service.advance_status(order, new_status, user=request.user, note=f"Marked {new_status} via web")
            messages.success(request, f"Order marked as {new_status.replace('_', ' ')}.")
        except order_service.OrderError as exc:
            messages.error(request, str(exc))
        return redirect("marketplace:order_detail", pk=pk)


class OrderCancelView(LoginRequiredMixin, View):
    def post(self, request, pk):
        order = get_object_or_404(Order, pk=pk)
        is_buyer = hasattr(request.user, "buyer_profile") and order.buyer_id == request.user.buyer_profile.id
        if not (is_buyer or request.user.is_staff):
            messages.error(request, "Only the buyer can cancel this order.")
            return redirect("marketplace:order_detail", pk=pk)
        try:
            order_service.advance_status(
                order, "cancelled", user=request.user, note=request.POST.get("reason", "Cancelled")
            )
            messages.success(request, "Order cancelled.")
        except order_service.OrderError as exc:
            messages.error(request, str(exc))
        return redirect("marketplace:order_detail", pk=pk)


# ===========================================================================
# Payments (Razorpay)
# ===========================================================================
class PaymentInitiateView(LoginRequiredMixin, View):
    def post(self, request, pk):
        order = get_object_or_404(Order, pk=pk)
        if not (hasattr(request.user, "buyer_profile") and order.buyer_id == request.user.buyer_profile.id):
            return JsonResponse({"error": "Not allowed"}, status=403)

        try:
            payment = payments.start_payment(order)
        except payments.PaymentError as exc:
            return JsonResponse({"error": str(exc)}, status=400)

        return JsonResponse(
            {
                "key_id": settings.RAZORPAY_KEY_ID,
                "provider_order_id": payment.provider_order_id,
                "amount": payments.to_paise(payment.amount),
                "currency": payment.currency,
                "order_id": order.pk,
                "name": settings.SITE_NAME,
                "verify_url": reverse("marketplace:payment_verify"),
            }
        )


class PaymentVerifyView(LoginRequiredMixin, View):
    """Browser callback from Razorpay Checkout — the fast path for good UX.

    The provider order id is matched against *this* order's payment as well as
    signature-checked, so a genuine payment cannot be replayed onto a different
    order.
    """

    def post(self, request):
        order = get_object_or_404(Order, pk=request.POST.get("order_id"))
        if not (hasattr(request.user, "buyer_profile") and order.buyer_id == request.user.buyer_profile.id):
            messages.error(request, "You are not allowed to pay for this order.")
            return redirect("marketplace:order_detail", pk=order.pk)

        try:
            payments.record_capture(
                order,
                provider_order_id=request.POST.get("razorpay_order_id", ""),
                payment_id=request.POST.get("razorpay_payment_id", ""),
                signature=request.POST.get("razorpay_signature", ""),
            )
        except payments.PaymentError as exc:
            messages.error(request, str(exc))
            return redirect("marketplace:order_detail", pk=order.pk)

        messages.success(request, "Payment received. Thank you!")
        return redirect("marketplace:order_detail", pk=order.pk)


class UpiPaidView(LoginRequiredMixin, View):
    """Buyer confirms they paid via direct UPI and records the UTR reference.

    This only records the *claim* (``Payment.status="authorized"``). Staff
    verify the credit in their UPI app and confirm it in the admin
    (``PaymentAdmin.confirm_upi``), which is what settles the order — so a
    false "I paid" cannot mark an order paid on its own.
    """

    def post(self, request, pk):
        order = get_object_or_404(Order, pk=pk)
        if not (hasattr(request.user, "buyer_profile") and order.buyer_id == request.user.buyer_profile.id):
            messages.error(request, "Only the buyer can record a payment for this order.")
            return redirect("marketplace:order_detail", pk=pk)
        if order.payment_status == "paid":
            messages.error(request, "This order is already paid.")
            return redirect("marketplace:order_detail", pk=pk)

        try:
            upi.record_claim(order, request.POST.get("utr", ""))
        except upi.UpiError as exc:
            messages.error(request, str(exc))
            return redirect("marketplace:order_detail", pk=order.pk)

        messages.success(
            request,
            "Payment recorded. It will be confirmed shortly — you can track the status on this page.",
        )
        return redirect("marketplace:order_detail", pk=pk)


class SellerPaymentRecordView(LoginRequiredMixin, View):
    """Buyer records the UTR of a direct payment to the farmer."""

    def post(self, request, pk):
        order = get_object_or_404(Order, pk=pk)
        if not (hasattr(request.user, "buyer_profile") and order.buyer_id == request.user.buyer_profile.id):
            messages.error(request, "Only the buyer can record this payment.")
            return redirect("marketplace:order_detail", pk=order.pk)
        try:
            upi.record_seller_payment(order, request.POST.get("utr", ""))
        except upi.UpiError as exc:
            messages.error(request, str(exc))
            return redirect("marketplace:order_detail", pk=order.pk)
        messages.success(
            request,
            "Recorded. The seller confirms the credit once it lands in their UPI app.",
        )
        return redirect("marketplace:order_detail", pk=order.pk)


class SellerPaymentConfirmView(LoginRequiredMixin, View):
    """Farmer confirms they received a direct payment."""

    def post(self, request, pk):
        payment = get_object_or_404(
            Payment.objects.select_related("order__listing__farmer"), pk=pk
        )
        try:
            upi.confirm_seller_receipt(payment, request.user)
            messages.success(request, "Payment confirmed. Order marked as paid.")
        except upi.UpiError as exc:
            messages.error(request, str(exc))
        return redirect("marketplace:order_detail", pk=payment.order_id)


@method_decorator(csrf_exempt, name="dispatch")
class PaymentWebhookView(View):
    """Razorpay server-to-server webhook. CSRF-exempt but signature-verified.

    Always answers 200 for anything it could process (including duplicates and
    unknown orders) so Razorpay stops retrying; only a bad signature or an
    unreadable body is an error.
    """

    MAX_BODY_BYTES = 64 * 1024

    def post(self, request):
        signature = request.headers.get("X-Razorpay-Signature", "")
        raw_body = request.body
        if not payments.verify_webhook_signature(raw_body, signature):
            return JsonResponse({"error": "invalid signature"}, status=400)
        if len(raw_body) > self.MAX_BODY_BYTES:
            return JsonResponse({"status": "ignored"})

        try:
            event = json.loads(raw_body or b"{}")
        except json.JSONDecodeError:
            logger.warning("Razorpay webhook arrived with an unreadable body")
            return JsonResponse({"status": "ignored"})
        if not isinstance(event, dict):
            return JsonResponse({"status": "ignored"})

        try:
            outcome = payments.handle_webhook_event(event)
        except Exception:  # pragma: no cover - never 500 into a retry loop
            logger.exception("Failed to apply Razorpay webhook event")
            return JsonResponse({"status": "error"}, status=500)

        return JsonResponse({"status": outcome})


class ReconciliationView(LoginRequiredMixin, UserPassesTestMixin, TemplateView):
    """Staff-only: match a bank statement against pending direct-UPI claims.

    Uploading a CSV produces a dry-run preview. Nothing is confirmed until a
    staff member presses "Confirm matched payments", so an ambiguous statement
    can never quietly settle orders.
    """

    template_name = "dashboard/reconcile.html"

    def test_func(self):
        return self.request.user.is_staff

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["runs"] = Reconciliation.objects.select_related("uploaded_by")[:20]
        context["pending_claims"] = Payment.objects.filter(
            provider="upi_direct", status="authorized"
        ).count()
        return context

    def post(self, request):
        reconciliation_id = request.POST.get("reconciliation")
        if reconciliation_id:
            return self._apply(request, reconciliation_id)
        return self._preview(request)

    def _preview(self, request):
        upload = request.FILES.get("statement")
        if upload is None:
            messages.error(request, "Choose a CSV file exported from your bank or UPI app.")
            return redirect("marketplace:payment_reconcile")

        try:
            record = reconciliation_service.preview_statement(
                upload, upload.name, user=request.user
            )
        except reconciliation_service.ReconciliationError as exc:
            messages.error(request, str(exc))
            return redirect("marketplace:payment_reconcile")

        if not record.total_rows:
            messages.warning(request, "No transaction references were found in that file.")
        elif record.matched_count:
            messages.success(
                request,
                f"{record.matched_count} of {record.total_rows} credits matched. "
                "Review the preview, then confirm.",
            )
        else:
            messages.warning(request, "Nothing matched. Check the unmatched rows below.")
        return redirect("marketplace:payment_reconcile_preview", pk=record.pk)

    def _apply(self, request, reconciliation_id):
        record = get_object_or_404(
            Reconciliation.objects.select_related("uploaded_by"), pk=reconciliation_id
        )
        if record.uploaded_by_id and record.uploaded_by_id != request.user.id:
            messages.error(request, "This statement was uploaded by someone else.")
            return redirect("marketplace:payment_reconcile_preview", pk=record.pk)
        try:
            result = reconciliation_service.apply_reconciliation(record)
        except reconciliation_service.ReconciliationError as exc:
            messages.error(request, str(exc))
            return redirect("marketplace:payment_reconcile_preview", pk=record.pk)

        messages.success(
            request,
            f"Confirmed {result['confirmed']} payment(s)."
            + (f" Skipped {len(result['skipped'])}: {', '.join(result['skipped'])}."
               if result["skipped"] else ""),
        )
        return redirect("marketplace:payment_reconcile_preview", pk=record.pk)


class ReconciliationPreviewView(LoginRequiredMixin, UserPassesTestMixin, DetailView):
    """Read-only view of one reconciliation: what matched, what did not."""

    model = Reconciliation
    template_name = "dashboard/reconcile_preview.html"
    context_object_name = "run"

    def test_func(self):
        return self.request.user.is_staff

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        report = self.object.report or {}
        context["matched"] = report.get("matched", [])
        context["unmatched"] = report.get("unmatched", [])
        context["reason_counts"] = reconciliation_service.unmatched_summary(self.object)
        return context


# ===========================================================================
# Demand board — buyers post needs, sellers respond
# ===========================================================================
class DemandRequestCreateView(LoginRequiredMixin, UserPassesTestMixin, FormView):
    """Buyer posts "I want 100 kg of tomato in Pune"."""

    template_name = "dashboard/request_form.html"
    form_class = forms.DemandRequestForm
    success_url = reverse_lazy("marketplace:my_requests")

    def test_func(self):
        return hasattr(self.request.user, "buyer_profile")

    def form_valid(self, form):
        try:
            request_obj = deals_service.create_request(
                buyer=self.request.user.buyer_profile,
                crop=form.cleaned_data["crop"],
                quantity=form.cleaned_data["quantity"],
                delivery_city=form.cleaned_data["delivery_city"],
                target_price=form.cleaned_data.get("target_price"),
                needed_by=form.cleaned_data.get("needed_by"),
                notes=form.cleaned_data.get("notes", ""),
            )
        except deals_service.DealError as exc:
            messages.error(self.request, str(exc))
            return self.form_invalid(form)

        messages.success(
            self.request,
            f"Request #{request_obj.pk} posted. Farmers selling "
            f"{request_obj.crop.name} can now respond to you.",
        )
        return redirect("marketplace:my_requests")


class DemandRequestListView(LoginRequiredMixin, UserPassesTestMixin, TemplateView):
    """The open board sellers browse."""

    template_name = "dashboard/requests.html"

    def test_func(self):
        return hasattr(self.request.user, "farmer_profile")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        farmer = self.request.user.farmer_profile
        crop = None
        if crop_id := self.request.GET.get("crop"):
            crop = Crop.objects.filter(pk=crop_id).first()

        requests_qs = deals_service.open_requests_for(farmer, crop=crop)
        context.update(
            {
                "requests": requests_qs[:100],
                "crops": Crop.objects.all(),
                "selected_crop": crop,
                "can_direct": deals_service.can_take_direct_payments(farmer),
            }
        )
        return context


class MyRequestsView(LoginRequiredMixin, UserPassesTestMixin, TemplateView):
    """A buyer's own requests and the offers received against them."""

    template_name = "dashboard/my_requests.html"

    def test_func(self):
        return hasattr(self.request.user, "buyer_profile")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        buyer = self.request.user.buyer_profile
        mine = (
            DemandRequest.objects.filter(buyer=buyer)
            .select_related("crop")
            .prefetch_related("offers__farmer__user")
        )
        context.update(
            {
                "requests": mine,
                "open_count": mine.filter(status="open").count(),
                "request_offers": RequestOffer.objects.filter(
                    request__buyer=buyer
                ).select_related("request__crop", "farmer", "order"),
            }
        )
        return context


class RequestDetailView(LoginRequiredMixin, DetailView):
    """One request: its offers, and the thread with each interested farmer."""

    model = DemandRequest
    template_name = "dashboard/request_detail.html"
    context_object_name = "request_obj"

    def get_queryset(self):
        user = self.request.user
        queryset = DemandRequest.objects.select_related("buyer__user", "crop")
        if hasattr(user, "buyer_profile"):
            return queryset.filter(buyer=user.buyer_profile)
        return queryset  # sellers may view any open request

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        user = self.request.user
        farmer = getattr(user, "farmer_profile", None)
        is_owner = self.object.buyer_id == getattr(
            getattr(user, "buyer_profile", None), "id", None
        )

        # Sellers only ever see their own offer on a request, never rivals'.
        offers = RequestOffer.objects.filter(request=self.object).select_related(
            "farmer__user", "order"
        )
        if farmer:
            offers = offers.filter(farmer=farmer)
        elif not is_owner:
            offers = RequestOffer.objects.none()

        conversations = Conversation.objects.none()
        if is_owner:
            conversations = Conversation.objects.filter(request=self.object)
        elif farmer is not None:
            conversations = Conversation.objects.filter(request=self.object, farmer=farmer)

        context.update(
            {
                "is_owner": is_owner,
                "is_farmer": farmer is not None,
                "offers": offers,
                "offer_form": (
                    forms.RequestOfferForm(
                        can_direct=deals_service.can_take_direct_payments(farmer)
                    )
                    if farmer and not is_owner and self.object.is_open
                    else None
                ),
                "conversations": conversations.select_related("farmer__user", "buyer__user"),
                "can_cancel": is_owner and self.object.is_open,
            }
        )
        return context


class RequestCloseView(LoginRequiredMixin, UserPassesTestMixin, View):
    def test_func(self):
        return hasattr(self.request.user, "buyer_profile")

    def post(self, request, pk):
        request_obj = get_object_or_404(DemandRequest, pk=pk)
        try:
            deals_service.close_request(request_obj, user=request.user)
            messages.success(request, "Request closed.")
        except deals_service.DealError as exc:
            messages.error(request, str(exc))
        return redirect("marketplace:my_requests")


class RequestOfferCreateView(LoginRequiredMixin, UserPassesTestMixin, View):
    """Farmer replies to a request with a price and a payment route."""

    def test_func(self):
        return hasattr(self.request.user, "farmer_profile")

    def post(self, request, pk):
        request_obj = get_object_or_404(DemandRequest, pk=pk)
        farmer = request.user.farmer_profile

        form = forms.RequestOfferForm(
            request.POST, can_direct=deals_service.can_take_direct_payments(farmer)
        )
        if not form.is_valid():
            for error in form.errors.values():
                messages.error(request, error.as_text())
            return redirect("marketplace:request_detail", pk=pk)

        try:
            offer = deals_service.create_offer(
                request_obj,
                farmer,
                price_per_unit=form.cleaned_data["price_per_unit"],
                quantity=form.cleaned_data["quantity"],
                payment_route=form.cleaned_data["payment_route"],
                message=form.cleaned_data.get("message", ""),
            )
        except deals_service.DealError as exc:
            messages.error(request, str(exc))
            return redirect("marketplace:request_detail", pk=pk)

        messages.success(
            request, f"Offer sent to {request_obj.buyer}. Contact details unlock if they accept."
        )
        return redirect("marketplace:request_detail", pk=offer.request_id)


class RequestOfferActionView(LoginRequiredMixin, View):
    """Buyer accepts/rejects, farmer withdraws."""

    def post(self, request, pk, action):
        offer = get_object_or_404(
            RequestOffer.objects.select_related("request", "farmer__user"), pk=pk
        )
        try:
            if action == "accept":
                offer = deals_service.respond_offer(offer, "accept", request.user)
                messages.success(
                    request,
                    f"Deal agreed — order #{offer.order_id} created. Contact details are now visible.",
                )
                return redirect("marketplace:order_detail", pk=offer.order_id)
            if action == "reject":
                deals_service.respond_offer(offer, "reject", request.user)
                messages.success(request, "Offer declined.")
            elif action == "withdraw":
                deals_service.withdraw_offer(offer, request.user)
                messages.success(request, "Offer withdrawn.")
            else:
                return HttpResponseBadRequest("Unknown action")
        except deals_service.DealError as exc:
            messages.error(request, str(exc))
        return redirect("marketplace:request_detail", pk=offer.request_id)


class ConversationListView(LoginRequiredMixin, TemplateView):
    template_name = "dashboard/conversations.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["conversations"] = deals_service.conversations_for(self.request.user)[
            :50
        ]
        return context


class ConversationView(LoginRequiredMixin, DetailView):
    """The message thread. Contact details appear only once a deal exists."""

    model = Conversation
    template_name = "dashboard/conversation.html"
    context_object_name = "conversation"

    def get_queryset(self):
        return Conversation.objects.select_related("buyer__user", "farmer__user", "request")

    def _is_participant(self, conversation) -> bool:
        user = self.request.user
        buyer = getattr(user, "buyer_profile", None)
        farmer = getattr(user, "farmer_profile", None)
        return (
            (buyer and conversation.buyer_id == buyer.id)
            or (farmer and conversation.farmer_id == farmer.id)
            or user.is_staff
        )

    def get(self, request, *args, **kwargs):
        self.object = self.get_object()
        if not self._is_participant(self.object):
            messages.error(request, "That conversation is not yours.")
            return redirect("marketplace:dashboard")
        return render(request, self.template_name, self.get_context_data())

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["message_form"] = forms.MessageForm()
        context["messages_list"] = self.object.messages.select_related("sender")[:200]
        context["contact"] = deals_service.contact_details(self.object, self.request.user)
        context["counterpart"] = (
            self.object.farmer if getattr(self.request.user, "buyer_profile", None)
            else self.object.buyer
        )
        context["is_farmer_side"] = bool(
            getattr(self.request.user, "farmer_profile", None)
            and self.request.user.farmer_profile.id == self.object.farmer_id
        )
        return context

    def post(self, request, *args, **kwargs):
        self.object = self.get_object()
        if not self._is_participant(self.object):
            messages.error(request, "That conversation is not yours.")
            return redirect("marketplace:dashboard")

        form = forms.MessageForm(request.POST)
        if form.is_valid():
            try:
                deals_service.post_message(self.object, request.user, form.cleaned_data["body"])
            except deals_service.DealError as exc:
                messages.error(request, str(exc))
        else:
            messages.error(request, "Write something first.")
        return redirect("marketplace:conversation", pk=self.object.pk)


class PayoutMethodView(LoginRequiredMixin, UserPassesTestMixin, TemplateView):
    """Sellers manage where their money should be sent."""

    template_name = "dashboard/payout_methods.html"

    def test_func(self):
        return hasattr(self.request.user, "farmer_profile")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        farmer = self.request.user.farmer_profile
        context.update(
            {
                "method": deals_service.payout_method_for(farmer),
                "form": forms.PayoutMethodForm(),
                "can_direct": deals_service.can_take_direct_payments(farmer),
                "platform_fee": settings.PLATFORM_FEE_PERCENT,
            }
        )
        return context

    def post(self, request):
        farmer = request.user.farmer_profile
        form = forms.PayoutMethodForm(request.POST)
        if not form.is_valid():
            messages.error(request, "Please fix the highlighted fields.")
            return redirect("marketplace:payout_methods")

        data = form.cleaned_data
        try:
            deals_service.save_payout_method(
                request.user,
                kind=data["kind"],
                upi_id=data.get("upi_id", ""),
                account_holder=data.get("account_holder", ""),
                account_number=data.get("account_number", ""),
                ifsc=data.get("ifsc", ""),
            )
        except deals_service.DealError as exc:
            messages.error(request, str(exc))
            return redirect("marketplace:payout_methods")

        messages.success(request, "Payout details saved.")
        return redirect("marketplace:payout_methods")


class EarningsView(LoginRequiredMixin, UserPassesTestMixin, TemplateView):
    """What a farmer has earned, what is waiting, and what has been paid out."""

    template_name = "dashboard/earnings.html"

    def test_func(self):
        return hasattr(self.request.user, "farmer_profile")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        farmer = self.request.user.farmer_profile
        context.update(
            {
                "farmer": farmer,
                "pending": payout_service.pending_summary(farmer),
                "totals": payout_service.farmer_totals(farmer),
                "payouts": Payout.objects.filter(farmer=farmer),
                "cutoff": payout_service.hold_cutoff(),
                "fee_percent": settings.PLATFORM_FEE_PERCENT,
                "destination": deals_service.payout_method_for(farmer),
            }
        )
        return context


class PartnerPayoutMethodView(LoginRequiredMixin, UserPassesTestMixin, TemplateView):
    """A transporter says where their delivery money should go.

    Separate from the farmer page because a partner has no ``User``-scoped
    payout details — the destination hangs off the ``DeliveryPartner`` row.
    """

    template_name = "dashboard/partner_payout_methods.html"

    def test_func(self):
        return hasattr(self.request.user, "delivery_partner")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        partner = self.request.user.delivery_partner
        context.update(
            {
                "partner": partner,
                "method": deals_service.partner_payout_method_for(partner),
                "form": forms.PayoutMethodForm(),
            }
        )
        return context

    def post(self, request):
        partner = request.user.delivery_partner
        form = forms.PayoutMethodForm(request.POST)
        if not form.is_valid():
            for error in form.errors.values():
                messages.error(request, error.as_text())
            return redirect("marketplace:partner_payout_methods")

        data = form.cleaned_data
        try:
            deals_service.save_partner_payout_method(
                partner,
                kind=data["kind"],
                upi_id=data.get("upi_id", ""),
                account_holder=data.get("account_holder", ""),
                account_number=data.get("account_number", ""),
                ifsc=data.get("ifsc", ""),
            )
        except deals_service.DealError as exc:
            messages.error(request, str(exc))
            return redirect("marketplace:partner_payout_methods")

        messages.success(request, "Payout details saved.")
        return redirect("marketplace:partner_payout_methods")


class PartnerEarningsView(LoginRequiredMixin, UserPassesTestMixin, TemplateView):
    """Delivery charges earned per completed shipment, and what has been paid."""

    template_name = "dashboard/partner_earnings.html"

    def test_func(self):
        return hasattr(self.request.user, "delivery_partner")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        partner = self.request.user.delivery_partner
        context.update(
            {
                "partner": partner,
                "pending": payout_service.pending_partner_summary(partner),
                "totals": payout_service.partner_totals(partner),
                "payouts": Payout.objects.filter(delivery_partner=partner),
                "cutoff": payout_service.hold_cutoff(),
                "destination": deals_service.partner_payout_method_for(partner),
            }
        )
        return context


class PayoutActionView(LoginRequiredMixin, UserPassesTestMixin, View):
    """Staff approve or settle a payout statement."""

    ACTIONS = {"approve", "pay", "cancel"}

    def test_func(self):
        return self.request.user.is_staff

    def post(self, request, pk, action):
        payout = get_object_or_404(
            Payout.objects.select_related("farmer", "delivery_partner"), pk=pk
        )
        try:
            if action == "approve":
                payout = payout_service.approve(payout, user=request.user)
                messages.success(request, f"Payout #{payout.pk} approved for ₹{payout.net_amount}.")
            elif action == "pay":
                payout = payout_service.mark_paid(
                    payout, request.POST.get("reference", ""), user=request.user
                )
                messages.success(request, f"Payout #{payout.pk} recorded as paid.")
            elif action == "cancel":
                payout = payout_service.cancel(
                    payout, user=request.user, reason=request.POST.get("reason", "")
                )
                messages.success(
                    request,
                    f"Payout #{payout.pk} cancelled — it returns to the next run.",
                )
            else:
                return HttpResponseBadRequest("Unknown action")
        except payout_service.PayoutError as exc:
            messages.error(request, str(exc))

        target = "marketplace:partner_earnings" if payout.is_partner_payout else "marketplace:earnings"
        return redirect(target)


# ===========================================================================
# Logistics
# ===========================================================================
class ShipmentListView(LoginRequiredMixin, TemplateView):
    template_name = "dashboard/shipment_list.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        user = self.request.user
        queryset = Shipment.objects.select_related("partner").prefetch_related("stops")
        if not user.is_staff:
            if hasattr(user, "delivery_partner"):
                queryset = queryset.filter(partner=user.delivery_partner)
            elif hasattr(user, "farmer_profile"):
                queryset = queryset.filter(stops__order__listing__farmer=user.farmer_profile).distinct()
            else:
                queryset = queryset.none()
        context["shipments"] = queryset
        if user.is_staff:
            context["assignable_orders"] = (
                Order.objects.filter(shipment_stops__isnull=True)
                .exclude(status__in=["cancelled", "paid"])
                .select_related("listing__crop", "listing__farmer", "buyer")[:50]
            )
        return context


class ShipmentDetailView(LoginRequiredMixin, DetailView):
    model = Shipment
    template_name = "dashboard/shipment_detail.html"
    context_object_name = "shipment"

    def get_queryset(self):
        return Shipment.objects.select_related("partner").prefetch_related(
            "stops__order", "stops__proof"
        )

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        user = self.request.user
        partner = getattr(user, "delivery_partner", None)
        open_deliveries = [
            stop for stop in self.object.stops.all()
            if stop.is_delivery and not hasattr(stop, "proof")
        ]
        context.update(
            {
                "can_record": bool(partner and self.object.partner_id == partner.pk)
                or user.is_staff,
                "proof_form": forms.DeliveryProofForm(),
                "open_deliveries": open_deliveries,
                # Which of those stops still has no code the buyer could read?
                # Those are the ones the driver needs to chase.
                "stops_needing_code": {
                    stop.order_id
                    for stop in open_deliveries
                    if stop.order_id and not DeliveryProof.objects.filter(
                        order_id=stop.order_id
                    ).exists()
                },
                "proofs": DeliveryProof.objects.filter(shipment=self.object).select_related(
                    "order", "recorded_by"
                ),
            }
        )
        return context


class PlanShipmentView(LoginRequiredMixin, UserPassesTestMixin, View):
    """Staff-only: plan an optimised shipment for selected orders."""

    def test_func(self):
        return self.request.user.is_staff

    def post(self, request):
        order_ids = request.POST.getlist("order_ids")
        if not order_ids:
            messages.error(request, "Select at least one order.")
            return redirect("marketplace:shipment_list")
        try:
            shipment = logistics.plan_shipment(order_ids)
        except logistics.LogisticsError as exc:
            messages.error(request, str(exc))
            return redirect("marketplace:shipment_list")
        messages.success(request, f"Shipment #{shipment.pk} planned.")
        return redirect("marketplace:shipment_detail", pk=shipment.pk)


# ===========================================================================
# Proof of delivery
# ===========================================================================
class _OrderParticipantMixin(LoginRequiredMixin, UserPassesTestMixin):
    """Restrict an order URL to its buyer, its farmer, its driver, or staff."""

    order: Order

    def get_order(self, pk=None):
        """Fetch (once) the order these URLs are pointed at.

        ``test_func`` needs the order and runs before ``post``/``get``, so the
        lookup lives here rather than in the handler.
        """
        pk = pk if pk is not None else self.kwargs["pk"]
        if getattr(self, "order", None) is None or self.order.pk != pk:
            self.order = get_object_or_404(
                Order.objects.select_related("listing__crop", "listing__farmer", "buyer"),
                pk=pk,
            )
        return self.order

    def test_func(self):
        order = self.get_order()
        user = self.request.user
        if user.is_staff:
            return True
        if getattr(user, "buyer_profile", None) and order.buyer_id == user.buyer_profile.id:
            return True
        if getattr(user, "farmer_profile", None) and order.listing.farmer_id == user.farmer_profile.id:
            return True
        # The transporter who drove it, if they have an account.
        partner = getattr(user, "delivery_partner", None)
        return bool(partner and order.shipment_stops.filter(
            shipment__partner=partner
        ).exists())


class DeliveryCodeView(_OrderParticipantMixin, View):
    """Issue a fresh delivery code to the buyer.

    The buyer normally asks for this themselves, but the **assigned driver can
    trigger it too**. Without that, a driver who turns up to a buyer who never
    clicked "send me my code" has no way forward at all — and the code is the
    only thing that lets them close the stop. Asking the driver to knock first
    is a much smaller failure than having no path whatsoever.
    """

    def post(self, request, pk):
        order = self.get_order(pk)
        partner = getattr(request.user, "delivery_partner", None)
        is_assigned_driver = bool(
            partner
            and order.shipment_stops.filter(shipment__partner=partner).exists()
        )
        if not (hasattr(request.user, "buyer_profile") or request.user.is_staff
                or is_assigned_driver):
            messages.error(request, "Only the buyer or their driver can issue a delivery code.")
            return redirect("marketplace:order_detail", pk=pk)

        try:
            code = delivery_service.issue_code(order)
        except delivery_service.DeliveryError as exc:
            messages.error(request, str(exc))
            return redirect("marketplace:order_detail", pk=pk)

        # Shown once, on the page that asked for it. It is emailed either way,
        # because the buyer may only ever see the mail.
        if settings.DEBUG:
            messages.success(request, f"Delivery code: {code}")
        else:
            messages.success(request, "We emailed the buyer a new delivery code.")
        if not is_assigned_driver or request.user.is_staff or hasattr(request.user, "buyer_profile"):
            request.session["delivery_code"] = code
        return redirect("marketplace:order_detail", pk=pk)


class DeliveryProofView(_OrderParticipantMixin, View):
    """The transporter closes a stop at the door."""

    def post(self, request, pk):
        order = self.get_order(pk)
        partner = getattr(request.user, "delivery_partner", None)
        if partner is None and not request.user.is_staff:
            messages.error(request, "Only the transporter or staff can record a delivery.")
            return redirect("marketplace:order_detail", pk=pk)

        form = forms.DeliveryProofForm(request.POST, request.FILES)
        if not form.is_valid():
            for error in form.errors.values():
                messages.error(request, error.as_text())
            return redirect("marketplace:order_detail", pk=pk)

        data = form.cleaned_data
        try:
            proof = delivery_service.record_proof(
                order,
                user=request.user,
                partner=partner,
                photo=data.get("photo"),
                signature=data.get("signature", ""),
                notes=data.get("notes", ""),
                latitude=data.get("latitude"),
                longitude=data.get("longitude"),
                code=data["code"],
            )
        except delivery_service.DeliveryError as exc:
            messages.error(request, str(exc))
            return redirect("marketplace:order_detail", pk=pk)

        messages.success(
            request,
            f"Delivery recorded for order #{order.pk} "
            f"({proof.evidence_count} piece(s) of evidence).",
        )
        return redirect("marketplace:shipment_detail", pk=proof.shipment_id)


class DeliveryConfirmView(_OrderParticipantMixin, View):
    """The buyer accepts the delivery, or disputes it."""

    def post(self, request, pk):
        order = self.get_order(pk)
        if not (hasattr(request.user, "buyer_profile") or request.user.is_staff):
            messages.error(request, "Only the buyer can respond to a delivery.")
            return redirect("marketplace:order_detail", pk=pk)

        action = request.POST.get("action", "confirm")
        try:
            if action == "dispute":
                form = forms.DeliveryDisputeForm(request.POST)
                if not form.is_valid():
                    messages.error(request, form.errors["reason"].as_text())
                    return redirect("marketplace:order_detail", pk=pk)
                delivery_service.confirm_by_buyer(
                    order, request.user, accept=False,
                    reason=form.cleaned_data["reason"],
                )
                messages.warning(request, "Disputed. The delivery charge is on hold.")
            else:
                delivery_service.confirm_by_buyer(order, request.user, accept=True)
                messages.success(request, "Delivery confirmed. Thank you!")
        except delivery_service.DeliveryError as exc:
            messages.error(request, str(exc))
        return redirect("marketplace:order_detail", pk=pk)


class ShipmentCompleteView(LoginRequiredMixin, UserPassesTestMixin, View):
    """Staff close a shipment whose stops can never be closed honestly.

    A buyer who has moved, a wrong address, produce handed to a gatekeeper with
    no code — the delivery happened but the paper trail cannot be completed.
    Forcing it through here is attributed and logged; hiding the escape hatch
    would only mean it gets used somewhere less visible.
    """

    def test_func(self):
        return self.request.user.is_staff

    def post(self, request, pk):
        shipment = get_object_or_404(Shipment, pk=pk)
        try:
            delivery_service.close_shipment(shipment, force=True, user=request.user)
        except delivery_service.DeliveryError as exc:
            messages.error(request, str(exc))
            return redirect("marketplace:shipment_detail", pk=pk)

        messages.success(
            request,
            f"Shipment #{shipment.pk} completed. Its delivery charge is now "
            "payable to the transporter.",
        )
        return redirect("marketplace:shipment_detail", pk=pk)


class DeliveryDisputeResolveView(LoginRequiredMixin, UserPassesTestMixin, View):
    """Staff decide a contested delivery: refund the buyer, or release the money."""

    ACTIONS = {"refund", "release"}

    def test_func(self):
        return self.request.user.is_staff

    def post(self, request, pk, action):
        if action not in self.ACTIONS:
            return HttpResponseBadRequest("Unknown action")
        order = get_object_or_404(Order.objects.select_related("listing__farmer"), pk=pk)
        try:
            delivery_service.resolve_dispute(
                order, request.user, action, notes=request.POST.get("notes", "")
            )
        except delivery_service.DeliveryError as exc:
            messages.error(request, str(exc))
            return redirect("marketplace:order_detail", pk=pk)

        if action == "release":
            messages.success(request, "Money released to the transporter.")
        else:
            messages.success(
                request, "Resolved in the buyer's favour — raise a refund for this order."
            )
        return redirect("marketplace:order_detail", pk=pk)


# ===========================================================================
# Forecasting
# ===========================================================================
class ForecastView(LoginRequiredMixin, TemplateView):
    template_name = "dashboard/forecast.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["crops"] = Crop.objects.all()
        context["recent"] = DemandForecast.objects.select_related("crop")[:10]
        return context


class ForecastRequestView(LoginRequiredMixin, View):
    """AJAX endpoint: forecast demand for a crop/region on request."""

    def post(self, request):
        crop = get_object_or_404(Crop, pk=request.POST.get("crop"))
        region = request.POST.get("region", "")
        try:
            horizon = int(request.POST.get("horizon_days", 7))
        except (TypeError, ValueError):
            horizon = 7
        result = forecasting.forecast_for_crop(crop, region, horizon_days=horizon)
        return JsonResponse(
            {
                "crop": result["crop"],
                "region": result["region"],
                "predicted_quantity": str(result["predicted_quantity"] or ""),
                "predicted_price_per_unit": str(result["predicted_price_per_unit"] or ""),
                "confidence": result["confidence"],
                "method": result["method"],
                "rationale": result["rationale"],
                "history": result["history"],
            }
        )


# ===========================================================================
# Price offers (buyer proposes → farmer approves)
# ===========================================================================
class OffersView(LoginRequiredMixin, TemplateView):
    """Offer inbox: farmers see offers received, buyers see offers they made."""
    template_name = "dashboard/offers.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        user = self.request.user
        status = self.request.GET.get("status", "")

        if hasattr(user, "farmer_profile"):
            queryset = PriceOffer.objects.filter(
                listing__farmer=user.farmer_profile
            ).select_related("listing__crop", "buyer", "order")
            context["role"] = "farmer"
        elif hasattr(user, "buyer_profile"):
            queryset = PriceOffer.objects.filter(buyer=user.buyer_profile).select_related(
                "listing__crop", "listing__farmer", "order"
            )
            context["role"] = "buyer"
        else:
            queryset = PriceOffer.objects.none()
            context["role"] = "none"

        if status in {"pending", "accepted", "rejected", "cancelled"}:
            queryset = queryset.filter(status=status)

        context["offers"] = queryset
        context["pending_count"] = queryset.filter(status="pending").count()
        context["status_filter"] = status
        return context


class OfferCreateView(LoginRequiredMixin, View):
    """A buyer sends a price offer to the farmer."""

    def post(self, request, pk):
        listing = get_object_or_404(Listing.objects.select_related("crop", "farmer"), pk=pk)
        if not hasattr(request.user, "buyer_profile"):
            messages.error(request, "Only buyers can make an offer.")
            return redirect("marketplace:listing_detail", pk=pk)
        if listing.farmer.user_id == request.user.id:
            messages.error(request, "You cannot make an offer on your own listing.")
            return redirect("marketplace:listing_detail", pk=pk)

        form = forms.OfferForm(request.POST, listing=listing)
        if not form.is_valid():
            for error in form.errors.values():
                messages.error(request, error.as_text())
            return redirect("marketplace:listing_detail", pk=pk)

        try:
            offer = offer_service.create_offer(
                listing=listing,
                buyer=request.user.buyer_profile,
                quantity=form.cleaned_data["quantity_requested"],
                proposed_price=form.cleaned_data["proposed_price"],
                message=form.cleaned_data.get("message", ""),
            )
        except offer_service.OfferError as exc:
            messages.error(request, str(exc))
            return redirect("marketplace:listing_detail", pk=pk)

        messages.success(
            request,
            f"Offer of ₹{offer.proposed_price} sent to {listing.farmer.full_name} — awaiting their approval.",
        )
        return redirect("marketplace:offers")


class OfferActionView(LoginRequiredMixin, View):
    """Accept / reject (farmer or staff) or withdraw (buyer) an offer."""

    def post(self, request, pk, action):
        offer = get_object_or_404(
            PriceOffer.objects.select_related("listing__farmer", "buyer", "order"), pk=pk
        )
        try:
            if action == "accept":
                offer = offer_service.respond_offer(offer, "accept", request.user)
                messages.success(request, f"Offer accepted — order #{offer.order_id} created.")
            elif action == "reject":
                offer_service.respond_offer(
                    offer, "reject", request.user, note=request.POST.get("note", "")
                )
                messages.success(request, "Offer rejected.")
            elif action == "cancel":
                offer_service.cancel_offer(offer, request.user)
                messages.success(request, "Offer withdrawn.")
            else:
                return HttpResponseBadRequest("Unknown action")
        except offer_service.OfferError as exc:
            messages.error(request, str(exc))
        return redirect("marketplace:offers")

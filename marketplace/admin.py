"""Django admin configuration."""

from django.contrib import admin
from django.contrib import messages

from marketplace.services import payments as payment_service
from marketplace.services import payouts as payout_service
from marketplace.services import upi as upi_service

from .models import (
    BuyerProfile,
    Conversation,
    DeliveryProof,
    Payout,
    PayoutItem,
    Crop,
    DeliveryPartner,
    DemandForecast,
    DemandRequest,
    FarmerProfile,
    Listing,
    Message,
    Order,
    OrderStatusHistory,
    Payment,
    PayoutMethod,
    PriceOffer,
    Reconciliation,
    RequestOffer,
    ServiceCheck,
    Shipment,
    ShipmentStop,
)


@admin.register(FarmerProfile)
class FarmerProfileAdmin(admin.ModelAdmin):
    list_display = ("full_name", "phone_number", "village", "district", "is_fpo", "kyc_verified")
    list_filter = ("is_fpo", "kyc_verified", "district", "state")
    search_fields = ("full_name", "phone_number", "village", "district")
    list_editable = ("kyc_verified",)


@admin.register(BuyerProfile)
class BuyerProfileAdmin(admin.ModelAdmin):
    list_display = ("__str__", "buyer_type", "phone_number", "city")
    list_filter = ("buyer_type", "city")
    search_fields = ("business_name", "phone_number", "user__username")


@admin.register(Crop)
class CropAdmin(admin.ModelAdmin):
    list_display = ("name", "unit", "category")
    search_fields = ("name",)
    list_filter = ("unit",)


@admin.register(Listing)
class ListingAdmin(admin.ModelAdmin):
    list_display = (
        "crop", "farmer", "quantity_available", "price_per_unit", "quality_grade",
        "status", "harvest_date",
    )
    list_filter = ("status", "quality_grade", "crop", "is_organic")
    search_fields = ("farmer__full_name", "crop__name")
    autocomplete_fields = ("crop",)
    date_hierarchy = "created_at"


class OrderStatusHistoryInline(admin.TabularInline):
    model = OrderStatusHistory
    extra = 0
    readonly_fields = ("status", "note", "changed_by", "changed_at")
    can_delete = False


class PaymentInline(admin.TabularInline):
    model = Payment
    extra = 0
    readonly_fields = ("provider_order_id", "provider_payment_id", "amount", "status", "method")


@admin.register(Order)
class OrderAdmin(admin.ModelAdmin):
    list_display = (
        "id", "listing", "buyer", "quantity_ordered", "status", "payment_status",
        "total_amount", "created_at",
    )
    list_filter = ("status", "payment_status", "created_at")
    search_fields = ("buyer__business_name", "listing__crop__name", "listing__farmer__full_name")
    inlines = [OrderStatusHistoryInline, PaymentInline]
    date_hierarchy = "created_at"
    readonly_fields = ("created_at", "updated_at")


@admin.register(Payment)
class PaymentAdmin(admin.ModelAdmin):
    list_display = ("order", "provider", "provider_order_id", "provider_payment_id", "amount", "currency", "status", "method", "created_at")
    list_filter = ("status", "provider", "method")
    search_fields = ("provider_order_id", "provider_payment_id", "order__id")
    readonly_fields = ("created_at", "updated_at")
    actions = ["confirm_upi", "refund_razorpay", "reverse_upi"]

    @admin.action(description="Confirm UPI payment (verify UTR, then mark the order paid)")
    def confirm_upi(self, request, queryset):
        """Settle direct-UPI payments whose UTR staff has verified in their UPI app."""
        confirmed, skipped = 0, 0
        for payment in queryset:
            try:
                upi_service.confirm_claim(payment)
            except upi_service.UpiError:
                skipped += 1
                continue
            confirmed += 1
        self.message_user(
            request,
            f"Confirmed {confirmed} UPI payment{'s' if confirmed != 1 else ''}."
            + (f" Skipped {skipped} (already handled or not a UPI claim)." if skipped else ""),
            level=messages.INFO if confirmed else messages.WARNING,
        )

    @admin.action(description="Refund via Razorpay and mark the order refunded")
    def refund_razorpay(self, request, queryset):
        """Issue a real refund through the gateway for captured online payments."""
        refunded, failed = 0, 0
        for payment in queryset:
            try:
                payment_service.refund_order_payment(
                    payment.order,
                    note=f"Refund issued from admin by {request.user}",
                )
            except payment_service.PaymentError as exc:
                failed += 1
                self.message_user(request, f"Payment #{payment.pk}: {exc}", level=messages.ERROR)
                continue
            refunded += 1
        if refunded:
            self.message_user(request, f"Refunded {refunded} payment(s) through Razorpay.")

    @admin.action(description="Record a manual UPI reversal (money sent back by hand)")
    def reverse_upi(self, request, queryset):
        """Direct UPI has no API to call — staff send the money, then record it here."""
        reversed_count, failed = 0, 0
        for payment in queryset:
            try:
                upi_service.reverse_claim(
                    payment, note=f"UPI reversal recorded by {request.user}"
                )
            except upi_service.UpiError:
                failed += 1
                continue
            reversed_count += 1
        self.message_user(
            request,
            f"Recorded {reversed_count} UPI reversal(s)."
            + (f" Skipped {failed} (not captured UPI payments)." if failed else ""),
            level=messages.INFO if reversed_count else messages.WARNING,
        )


@admin.register(DemandRequest)
class DemandRequestAdmin(admin.ModelAdmin):
    list_display = ("id", "crop", "quantity", "buyer", "delivery_city", "status", "created_at")
    list_filter = ("status", "crop")
    search_fields = ("buyer__business_name", "crop__name", "delivery_city")
    date_hierarchy = "created_at"


class RequestOfferInline(admin.TabularInline):
    model = RequestOffer
    extra = 0
    readonly_fields = ("farmer", "quantity", "price_per_unit", "payment_route", "status", "order")


@admin.register(RequestOffer)
class RequestOfferAdmin(admin.ModelAdmin):
    list_display = (
        "id", "request", "farmer", "quantity", "price_per_unit",
        "payment_route", "status", "responded_at",
    )
    list_filter = ("status", "payment_route", "request__crop")
    search_fields = ("farmer__full_name", "request__delivery_city")
    readonly_fields = ("created_at", "updated_at", "responded_at")


@admin.register(Conversation)
class ConversationAdmin(admin.ModelAdmin):
    list_display = ("id", "buyer", "farmer", "request", "order", "last_message_at")
    search_fields = ("buyer__business_name", "farmer__full_name")


@admin.register(Message)
class MessageAdmin(admin.ModelAdmin):
    list_display = ("id", "conversation", "sender", "created_at")
    search_fields = ("body", "sender__username")
    readonly_fields = ("conversation", "sender", "body", "created_at")


@admin.register(PayoutMethod)
class PayoutMethodAdmin(admin.ModelAdmin):
    list_display = (
        "id", "user", "delivery_partner", "kind", "masked_account_number",
        "upi_id", "ifsc", "is_verified",
    )
    list_filter = ("kind", "is_verified", "is_primary")
    search_fields = ("user__username", "delivery_partner__name", "upi_id", "account_holder")
    readonly_fields = ("created_at", "updated_at")
    actions = ["verify_methods"]

    @admin.action(description="Mark selected payout methods as verified")
    def verify_methods(self, request, queryset):
        updated = queryset.update(is_verified=True)
        self.message_user(request, f"Verified {updated} payout method(s).")


class PayoutItemInline(admin.TabularInline):
    model = PayoutItem
    extra = 0
    readonly_fields = ("order", "shipment", "amount", "commission", "delivery_fee", "settled_at")
    can_delete = False


@admin.register(Payout)
class PayoutAdmin(admin.ModelAdmin):
    list_display = (
        "id", "recipient_name", "period_start", "period_end", "order_count",
        "net_amount", "status", "reference", "paid_at",
    )
    list_filter = ("status", "method")
    search_fields = ("farmer__full_name", "delivery_partner__name", "reference")
    readonly_fields = (
        "farmer", "delivery_partner", "period_start", "period_end", "order_count",
        "gross_amount", "commission_amount", "delivery_amount", "net_amount", "approved_at",
        "paid_at", "processed_by", "created_at", "updated_at",
    )
    date_hierarchy = "created_at"
    inlines = [PayoutItemInline]
    actions = ["approve_payouts", "mark_paid", "cancel_payouts"]

    @admin.action(description="Approve selected payouts for payment")
    def approve_payouts(self, request, queryset):
        approved, failed = 0, []
        for payout in queryset.filter(status="draft"):
            try:
                payout_service.approve(payout, user=request.user)
                approved += 1
            except payout_service.PayoutError as exc:
                failed.append(f"#{payout.pk}: {exc}")
        self._report(request, approved, failed, "approved")

    @admin.action(description="Record selected payouts as paid (add reference in the form)")
    def mark_paid(self, request, queryset):
        """Each payout needs its own bank reference, so this opens a form."""
        if not queryset.filter(status="approved").exists():
            self.message_user(
                request, "Only approved payouts can be recorded as paid.", level=messages.WARNING
            )
            return
        return self._payout_form(request, queryset.filter(status="approved"), "paid")

    @admin.action(description="Cancel payouts and release their items")
    def cancel_payouts(self, request, queryset):
        cancelled, failed = 0, []
        for payout in queryset.exclude(status__in=["paid", "cancelled"]):
            try:
                payout_service.cancel(payout, user=request.user, reason="cancelled from admin")
                cancelled += 1
            except payout_service.PayoutError as exc:
                failed.append(f"#{payout.pk}: {exc}")
        self._report(request, cancelled, failed, "cancelled")

    def _report(self, request, count, failures, verb):
        self.message_user(
            request,
            f"{count} payout(s) {verb}."
            + (f" Failed: {'; '.join(failures)}" if failures else ""),
            level=messages.INFO if count else messages.WARNING,
        )

    def _payout_form(self, request, queryset, verb):
        from django import forms
        from django.shortcuts import redirect

        options = queryset
        total = sum((p.net_amount for p in options), 0)

        class ReferenceForm(forms.Form):
            reference = forms.CharField(
                max_length=100,
                help_text=f"Bank/gateway reference. Marks {options.count()} payout(s) "
                          f"totalling ₹{total} as paid. A per-payout value below "
                          f"overrides this one.",
            )

        if "reference" in request.POST:
            form = ReferenceForm(request.POST)
            if form.is_valid():
                shared = form.cleaned_data["reference"].strip()
                recorded = 0
                for payout in options:
                    reference = request.POST.get(f"reference_{payout.pk}", "").strip() or shared
                    try:
                        payout_service.mark_paid(payout, reference, user=request.user)
                        recorded += 1
                    except payout_service.PayoutError:
                        continue
                self.message_user(request, f"Recorded {recorded} payout(s) as paid.")
                return None
        else:
            form = ReferenceForm()

        from django.template.response import TemplateResponse

        context = {
            **self.admin_site.each_context(request),
            "title": f"Record {options.count()} payout(s) as paid",
            "form": form,
            "options": options,
            "total": total,
            "opts": self.model._meta,
        }
        return TemplateResponse(request, "admin/payout_paid_form.html", context)


@admin.register(DeliveryProof)
class DeliveryProofAdmin(admin.ModelAdmin):
    list_display = (
        "id", "order", "shipment", "status", "evidence", "recorded_by",
        "created_at",
    )
    list_filter = ("status", "shipment__partner")
    search_fields = ("order__listing__farmer__full_name", "notes", "dispute_reason")
    readonly_fields = (
        "otp_hash", "otp_sent_at", "otp_confirmed_at", "otp_attempts",
        "stop", "order", "shipment", "created_at", "updated_at",
    )
    date_hierarchy = "created_at"
    actions = ["confirm_proofs", "reopen_proofs"]

    @admin.display(description="Evidence")
    def evidence(self, obj):
        return obj.evidence_count

    @admin.action(description="Mark selected proofs confirmed")
    def confirm_proofs(self, request, queryset):
        updated = queryset.exclude(status="confirmed").update(
            status="confirmed", dispute_reason=""
        )
        self.message_user(request, f"Confirmed {updated} proof(s).")

    @admin.action(description="Reopen selected proofs for review")
    def reopen_proofs(self, request, queryset):
        updated = queryset.filter(status="confirmed").update(status="recorded")
        self.message_user(request, f"Reopened {updated} proof(s).")


@admin.register(Reconciliation)
class ReconciliationAdmin(admin.ModelAdmin):
    list_display = (
        "id", "filename", "total_rows", "matched_count", "unmatched_count",
        "applied", "uploaded_by", "created_at",
    )
    list_filter = ("applied", "created_at")
    search_fields = ("filename", "uploaded_by__username")
    readonly_fields = (
        "filename", "uploaded_by", "total_rows", "matched_count",
        "unmatched_count", "applied", "applied_at", "report", "created_at", "updated_at",
    )
    date_hierarchy = "created_at"

    def has_add_permission(self, request):
        # Statements are uploaded through the reconciliation page so the
        # preview/apply flow is always used.
        return False


@admin.register(DeliveryPartner)
class DeliveryPartnerAdmin(admin.ModelAdmin):
    list_display = ("name", "phone_number", "vehicle_type", "capacity_kg", "is_available")
    list_filter = ("vehicle_type", "is_available")
    search_fields = ("name", "phone_number")


class PartnerShipmentActionsMixin:
    """Shipments are money now, so they get the same lifecycle actions."""

    @admin.action(description="Mark selected shipments as completed")
    def complete(self, request, queryset):
        # Routed through the delivery service so the stops are closed and the
        # action is logged, rather than a bare UPDATE.
        from marketplace.services import delivery as delivery_service

        done, failed = 0, []
        for shipment in queryset.exclude(status__in=["completed", "cancelled"]):
            try:
                delivery_service.close_shipment(shipment, force=True, user=request.user)
                done += 1
            except delivery_service.DeliveryError as exc:
                failed.append(f"#{shipment.pk}: {exc}")
        self.message_user(
            request,
            f"{done} shipment(s) completed."
            + (f" Failed: {'; '.join(failed)}" if failed else ""),
            level=messages.INFO if done else messages.WARNING,
        )

    @admin.action(description="Mark selected shipments as dispatched")
    def dispatch(self, request, queryset):
        updated = queryset.filter(status="planned").update(status="dispatched")
        self.message_user(request, f"{updated} shipment(s) marked dispatched.")

    @admin.action(description="Cancel selected shipments (their delivery charge is not owed)")
    def cancel(self, request, queryset):
        updated = queryset.exclude(status="completed").update(status="cancelled")
        self.message_user(request, f"{updated} shipment(s) cancelled.")


class ShipmentStopInline(admin.TabularInline):
    model = ShipmentStop
    extra = 0


@admin.register(Shipment)
class ShipmentAdmin(PartnerShipmentActionsMixin, admin.ModelAdmin):
    list_display = ("id", "partner", "status", "planned_distance_km", "planned_cost", "optimized_at")
    list_filter = ("status",)
    inlines = [ShipmentStopInline]
    actions = ["dispatch", "complete", "cancel"]
    readonly_fields = ("planned_distance_km", "planned_cost", "optimized_at", "route_notes")


@admin.register(PriceOffer)
class PriceOfferAdmin(admin.ModelAdmin):
    list_display = (
        "listing", "buyer", "quantity_requested", "proposed_price",
        "status", "responded_at", "created_at",
    )
    list_filter = ("status", "listing__crop")
    search_fields = ("buyer__business_name", "listing__farmer__full_name", "listing__crop__name")
    readonly_fields = ("created_at", "updated_at", "responded_at")
    autocomplete_fields = ("listing",)


@admin.register(DemandForecast)
class DemandForecastAdmin(admin.ModelAdmin):
    list_display = (
        "crop", "region", "horizon_days", "predicted_quantity",
        "predicted_price_per_unit", "confidence", "method", "created_at",
    )
    list_filter = ("method", "crop", "region")
    search_fields = ("crop__name", "region")
    readonly_fields = ("created_at",)


@admin.register(ServiceCheck)
class ServiceCheckAdmin(admin.ModelAdmin):
    """Read-only: these rows are written by the watchdog, not by hand."""

    list_display = ("checked_at", "name", "severity", "detail", "is_transition")
    list_filter = ("severity", "name", "is_transition")
    search_fields = ("name", "detail")
    readonly_fields = ("name", "severity", "detail", "is_transition", "checked_at")
    date_hierarchy = "checked_at"

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

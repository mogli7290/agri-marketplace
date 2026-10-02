"""Template context shared across every page."""

from django.conf import settings


def site_context(request):
    """Expose the site name, the user's marketplace profile, and pending offers."""
    farmer = getattr(request.user, "farmer_profile", None) if request.user.is_authenticated else None
    buyer = getattr(request.user, "buyer_profile", None) if request.user.is_authenticated else None
    partner = (
        getattr(request.user, "delivery_partner", None)
        if request.user.is_authenticated
        else None
    )

    pending_offers = 0
    if farmer or buyer:
        from marketplace.models import PriceOffer

        pending = PriceOffer.objects.filter(status="pending")
        if farmer:
            pending_offers = pending.filter(listing__farmer=farmer).count()
        elif buyer:
            pending_offers = pending.filter(buyer=buyer).count()

    pending_upi_claims = 0
    if request.user.is_authenticated and request.user.is_staff and settings.UPI_ID:
        from marketplace.models import Payment

        pending_upi_claims = Payment.objects.filter(
            provider="upi_direct", status="authorized"
        ).count()

    return {
        "site_name": settings.SITE_NAME,
        "farmer_profile": farmer,
        "buyer_profile": buyer,
        "delivery_partner_profile": partner,
        "pending_offer_count": pending_offers,
        "upi_configured": bool(settings.UPI_ID),
        "pending_upi_claims": pending_upi_claims,
    }

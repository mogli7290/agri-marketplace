"""Domain models for the agri-marketplace.

The schema is organised around four concerns:

* **Participants** — ``FarmerProfile`` (farmers / FPOs) and ``BuyerProfile``
  (consumers / bulk buyers), each optionally carrying a geographic point used
  by the logistics and route-optimisation services.
* **Supply** — ``Crop`` and ``Listing`` (a farmer's offer to sell a quantity).
* **Demand** — ``Order`` and its auditable ``OrderStatusHistory``, plus
  ``Payment`` for online settlement.
* **Logistics & intelligence** — ``DeliveryPartner``, ``Shipment`` and
  ``ShipmentStop`` for movement, and ``DemandForecast`` for AI-generated
  demand/pricing signals.
"""

from decimal import Decimal

from django.conf import settings
from django.db import models
from django.utils import timezone


class TimeStampedModel(models.Model):
    """Abstract base giving models consistent creation/update timestamps."""

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


#: Where the buyer's money goes. The seller picks per deal:
#:   platform - buyer pays the marketplace, which keeps its fee and settles the
#:              farmer later (the only route that earns the platform a fee)
#:   direct   - buyer pays the farmer's own UPI ID; the platform is not a party
#:              to the payment and earns nothing, but the farmer gets 100%
#:               immediately.
#:
#: Module-level because both ``Order`` and ``FarmerProfile`` need it, and
#: FarmerProfile is declared long before Order.
PAYMENT_ROUTE_CHOICES = [
    ("platform", "Through the platform"),
    ("direct", "Direct to the farmer"),
]


# ---------------------------------------------------------------------------
# Participants
# ---------------------------------------------------------------------------
class FarmerProfile(TimeStampedModel):
    """A farmer or an FPO (Farmer Producer Organisation) selling produce."""

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="farmer_profile"
    )
    is_fpo = models.BooleanField(
        default=False, help_text="True if this account represents an FPO, not an individual farmer"
    )
    full_name = models.CharField(max_length=150)
    phone_number = models.CharField(max_length=20, unique=True)
    village = models.CharField(max_length=150)
    district = models.CharField(max_length=150)
    state = models.CharField(max_length=150)
    preferred_language = models.CharField(max_length=50, default="en")
    latitude = models.DecimalField(
        max_digits=9, decimal_places=6, null=True, blank=True,
        help_text="Pickup location latitude, used for route optimisation",
    )
    longitude = models.DecimalField(
        max_digits=9, decimal_places=6, null=True, blank=True,
        help_text="Pickup location longitude, used for route optimisation",
    )
    kyc_verified = models.BooleanField(default=False)
    preferred_payment_route = models.CharField(
        max_length=10,
        choices=PAYMENT_ROUTE_CHOICES,
        default="platform",
        help_text="How buyers pay this farmer by default. Direct needs a UPI ID on file.",
    )

    class Meta:
        ordering = ["full_name"]
        indexes = [models.Index(fields=["district", "state"])]

    def __str__(self):
        return f"{self.full_name} ({self.village}, {self.district})"


class EmailVerification(TimeStampedModel):
    """Proof that a registered user controls the email address they signed up with.

    Created when somebody registers. ``verified_at`` is stamped when the link is
    followed; until then the account may not log in (when
    ``settings.EMAIL_VERIFICATION_REQUIRED`` is on).

    Only the *hash* of the token is stored, so a leaked database cannot be used
    to verify accounts, and issuing a new link invalidates the previous one.
    Accounts created out of band — an admin, ``createsuperuser``, seed data — have
    no row here and are treated as already verified.
    """

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
        related_name="email_verification",
    )
    token_hash = models.CharField(max_length=64, blank=True, db_index=True)
    sent_at = models.DateTimeField(default=timezone.now)
    verified_at = models.DateTimeField(null=True, blank=True)
    send_count = models.PositiveIntegerField(default=0)

    class Meta:
        verbose_name = "Email verification"

    @property
    def is_verified(self) -> bool:
        return self.verified_at is not None

    def can_resend(self) -> bool:
        """Rate limit: one link per cooldown window."""
        elapsed = (timezone.now() - self.sent_at).total_seconds()
        return elapsed >= settings.EMAIL_VERIFICATION_RESEND_COOLDOWN

    def __str__(self):
        return f"{self.user} ({'verified' if self.is_verified else 'pending'})"


class BuyerProfile(TimeStampedModel):
    """A consumer or a bulk buyer (restaurant, retailer, processor)."""

    BUYER_TYPE_CHOICES = [
        ("consumer", "Consumer"),
        ("bulk", "Bulk buyer"),
    ]

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="buyer_profile"
    )
    buyer_type = models.CharField(max_length=20, choices=BUYER_TYPE_CHOICES, default="consumer")
    business_name = models.CharField(max_length=150, blank=True)
    phone_number = models.CharField(max_length=20, unique=True)
    city = models.CharField(max_length=150)
    address = models.TextField(blank=True)
    latitude = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)
    longitude = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return self.business_name or self.user.get_full_name() or self.phone_number


# ---------------------------------------------------------------------------
# Supply
# ---------------------------------------------------------------------------
class Crop(models.Model):
    """A crop type, e.g. Tomato, Onion — kept separate so pricing/forecasting can key off it."""

    name = models.CharField(max_length=100, unique=True)
    unit = models.CharField(max_length=20, default="kg", help_text="kg, quintal, dozen, etc.")
    category = models.CharField(max_length=100, blank=True)

    #: Artwork shown when a listing has no photo. Keyed on a substring of the
    #: crop name so farmer-entered variants ("Tomato (Hybrid)") still match.
    #: Longer keywords are tested first, so "Sweet Potato" gets the potato art
    #: rather than falling through to the generic crate.
    PLACEHOLDER_IMAGES = {
        "groundnut": "img/crops/groundnut.svg",
        "soybean": "img/crops/soybean.svg",
        "soya bean": "img/crops/soybean.svg",
        "potato": "img/crops/potato.svg",
        "tomato": "img/crops/tomato.svg",
        "onion": "img/crops/onion.svg",
        "shallot": "img/crops/onion.svg",
        "banana": "img/crops/banana.svg",
        "plantain": "img/crops/banana.svg",
        "mango": "img/crops/mango.svg",
        "wheat": "img/crops/wheat.svg",
        "rice": "img/crops/rice.svg",
        "paddy": "img/crops/rice.svg",
        "maize": "img/crops/maize.svg",
        "corn": "img/crops/maize.svg",
    }
    DEFAULT_PLACEHOLDER_IMAGE = "img/listing-placeholder.svg"

    #: Sorted longest-first so the most specific keyword wins.
    PLACEHOLDER_KEYWORDS = sorted(PLACEHOLDER_IMAGES, key=len, reverse=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name

    @property
    def placeholder_image(self) -> str:
        """Static path to this crop's placeholder art, for listings with no photo."""
        name = self.name.lower()
        for keyword in self.PLACEHOLDER_KEYWORDS:
            if keyword in name:
                return self.PLACEHOLDER_IMAGES[keyword]
        return self.DEFAULT_PLACEHOLDER_IMAGE


class Listing(TimeStampedModel):
    """A farmer's offer to sell a quantity of a crop."""

    QUALITY_GRADE_CHOICES = [
        ("A", "Grade A"),
        ("B", "Grade B"),
        ("C", "Grade C"),
    ]
    STATUS_CHOICES = [
        ("active", "Active"),
        ("sold_out", "Sold out"),
        ("expired", "Expired"),
        ("cancelled", "Cancelled"),
    ]

    farmer = models.ForeignKey(FarmerProfile, on_delete=models.CASCADE, related_name="listings")
    crop = models.ForeignKey(Crop, on_delete=models.PROTECT, related_name="listings")
    quantity_available = models.DecimalField(max_digits=10, decimal_places=2)
    quality_grade = models.CharField(max_length=1, choices=QUALITY_GRADE_CHOICES, default="A")
    price_per_unit = models.DecimalField(max_digits=10, decimal_places=2)
    ai_suggested_price = models.DecimalField(
        max_digits=10, decimal_places=2, null=True, blank=True,
        help_text="Price recommendation produced by the forecasting service",
    )
    harvest_date = models.DateField()
    photo = models.ImageField(upload_to="listing_photos/", blank=True, null=True)
    description = models.TextField(blank=True)
    is_organic = models.BooleanField(default=False)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="active")

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["status", "crop"]),
            models.Index(fields=["-created_at"]),
        ]

    def __str__(self):
        return f"{self.crop} - {self.quantity_available}{self.crop.unit} by {self.farmer}"

    @property
    def is_orderable(self) -> bool:
        return self.status == "active" and self.quantity_available > 0


# ---------------------------------------------------------------------------
# Demand
# ---------------------------------------------------------------------------
class Order(TimeStampedModel):
    """An order placed by a buyer against one listing."""

    STATUS_CHOICES = [
        ("placed", "Placed"),
        ("confirmed", "Confirmed"),
        ("picked_up", "Picked up"),
        ("in_transit", "In transit"),
        ("delivered", "Delivered"),
        ("paid", "Paid"),
        ("cancelled", "Cancelled"),
    ]
    PAYMENT_STATUS_CHOICES = [
        ("pending", "Pending"),
        ("paid", "Paid"),
        ("failed", "Failed"),
        ("refunded", "Refunded"),
    ]
    # Where the buyer's money goes. The seller picks per deal; see
    # PAYMENT_ROUTE_CHOICES at the top of this module.
    PAYMENT_ROUTE_CHOICES = PAYMENT_ROUTE_CHOICES

    # Allowed forward transitions; enforced in the service layer and API.
    STATUS_TRANSITIONS = {
        "placed": ["confirmed", "cancelled"],
        "confirmed": ["picked_up", "cancelled"],
        "picked_up": ["in_transit"],
        "in_transit": ["delivered"],
        "delivered": ["paid"],
        "paid": [],
        "cancelled": [],
    }

    buyer = models.ForeignKey(BuyerProfile, on_delete=models.CASCADE, related_name="orders")
    listing = models.ForeignKey(Listing, on_delete=models.PROTECT, related_name="orders")
    quantity_ordered = models.DecimalField(max_digits=10, decimal_places=2)
    agreed_price_per_unit = models.DecimalField(max_digits=10, decimal_places=2)
    platform_fee = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    delivery_fee = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    payment_route = models.CharField(
        max_length=10, choices=PAYMENT_ROUTE_CHOICES, default="platform",
        help_text="Direct transfers carry no platform fee",
    )
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="placed")
    payment_status = models.CharField(
        max_length=20, choices=PAYMENT_STATUS_CHOICES, default="pending"
    )
    pickup_hub = models.CharField(max_length=150, blank=True)
    delivery_address = models.TextField(blank=True)
    delivery_latitude = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)
    delivery_longitude = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)
    cancellation_reason = models.CharField(max_length=255, blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["status"]),
            models.Index(fields=["payment_status"]),
        ]

    @property
    def subtotal(self) -> Decimal:
        return self.quantity_ordered * self.agreed_price_per_unit

    @property
    def total_amount(self) -> Decimal:
        return self.subtotal + self.platform_fee + self.delivery_fee

    @property
    def farmer_payout(self) -> Decimal:
        return self.subtotal

    def can_transition_to(self, new_status: str) -> bool:
        return new_status in self.STATUS_TRANSITIONS.get(self.status, [])

    def __str__(self):
        return f"Order #{self.id} - {self.listing.crop} x{self.quantity_ordered}"


class OrderStatusHistory(models.Model):
    """Audit trail of order status changes, useful for tracking and for training data later."""

    order = models.ForeignKey(Order, on_delete=models.CASCADE, related_name="status_history")
    status = models.CharField(max_length=20, choices=Order.STATUS_CHOICES)
    note = models.CharField(max_length=255, blank=True)
    changed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True,
        on_delete=models.SET_NULL, related_name="order_status_changes",
    )
    changed_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["changed_at"]
        verbose_name_plural = "Order status histories"

    def __str__(self):
        return f"{self.order} -> {self.status}"


class PriceOffer(TimeStampedModel):
    """A buyer's proposed price for a listing, awaiting the farmer's approval.

    Accepting converts the offer into an :class:`Order` at the agreed price, so
    buyers can negotiate instead of setting a price unilaterally.
    """

    STATUS_CHOICES = [
        ("pending", "Pending"),
        ("accepted", "Accepted"),
        ("rejected", "Rejected"),
        ("cancelled", "Cancelled"),
    ]

    listing = models.ForeignKey(Listing, on_delete=models.CASCADE, related_name="offers")
    buyer = models.ForeignKey(
        BuyerProfile, on_delete=models.CASCADE, related_name="price_offers"
    )
    quantity_requested = models.DecimalField(max_digits=10, decimal_places=2)
    proposed_price = models.DecimalField(
        max_digits=10, decimal_places=2,
        help_text="Price per unit the buyer is offering",
    )
    message = models.CharField(max_length=500, blank=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="pending")
    response_note = models.CharField(max_length=255, blank=True)
    responded_at = models.DateTimeField(null=True, blank=True)
    order = models.OneToOneField(
        Order, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="price_offer",
    )

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["listing", "status"])]
        constraints = [
            models.UniqueConstraint(
                fields=["listing", "buyer"],
                condition=models.Q(status="pending"),
                name="unique_pending_offer_per_buyer",
            )
        ]

    @property
    def unit_saving(self) -> Decimal:
        """Difference between the list price and the offered price (negative = asking less)."""
        return (self.proposed_price - self.listing.price_per_unit).quantize(Decimal("0.01"))

    def __str__(self):
        return f"Offer ₹{self.proposed_price} on {self.listing.crop} ({self.status})"


class Payment(TimeStampedModel):
    """A payment attempt against an order.

    Covers both settlement routes: ``provider="razorpay"`` for gateway payments
    (UPI/card/netbanking) and ``provider="upi_direct"`` for money sent straight
    to the platform's UPI ID, where ``provider_payment_id`` holds the buyer's
    UTR.
    """

    STATUS_CHOICES = [
        ("created", "Created"),
        ("authorized", "Authorized"),
        ("captured", "Captured"),
        ("failed", "Failed"),
        ("refunded", "Refunded"),
    ]

    order = models.ForeignKey(Order, on_delete=models.CASCADE, related_name="payments")
    provider = models.CharField(max_length=30, default="razorpay")
    provider_order_id = models.CharField(max_length=100, blank=True, db_index=True)
    provider_payment_id = models.CharField(max_length=100, blank=True, db_index=True)
    signature = models.CharField(max_length=255, blank=True)
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    currency = models.CharField(max_length=10, default="INR")
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="created")
    method = models.CharField(max_length=40, blank=True)
    failure_reason = models.CharField(max_length=255, blank=True)
    reconciliation = models.ForeignKey(
        "Reconciliation", null=True, blank=True, on_delete=models.SET_NULL,
        related_name="payments", help_text="Bank statement this claim was matched against",
    )
    reconciled_at = models.DateTimeField(
        null=True, blank=True, help_text="When a staff member confirmed the UPI credit"
    )

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["provider", "status"])]
        constraints = [
            # A provider reference identifies one real payment: the same UTR or
            # gateway payment id can never settle two different orders.
            models.UniqueConstraint(
                fields=["provider", "provider_payment_id"],
                condition=~models.Q(provider_payment_id=""),
                name="unique_provider_reference",
            )
        ]

    def __str__(self):
        return f"Payment {self.provider_payment_id or self.provider_order_id} ({self.status})"


# ---------------------------------------------------------------------------
# Demand board: buyers post what they need, sellers come to them
# ---------------------------------------------------------------------------
class DemandRequest(TimeStampedModel):
    """A buyer's public request for produce — "I want 100 kg of tomato"."""

    STATUS_CHOICES = [
        ("open", "Open"),
        ("fulfilled", "Fulfilled"),
        ("cancelled", "Cancelled"),
    ]

    buyer = models.ForeignKey(BuyerProfile, on_delete=models.CASCADE, related_name="requests")
    crop = models.ForeignKey(Crop, on_delete=models.PROTECT, related_name="requests")
    quantity = models.DecimalField(max_digits=10, decimal_places=2)
    target_price = models.DecimalField(
        max_digits=10, decimal_places=2, null=True, blank=True,
        help_text="Price per unit the buyer hopes for (advisory, not binding)",
    )
    delivery_city = models.CharField(max_length=150)
    needed_by = models.DateField(null=True, blank=True)
    notes = models.TextField(blank=True, help_text="Grade, organic, packing, anything else")
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="open")

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["status", "crop"]),
            models.Index(fields=["-created_at"]),
        ]

    @property
    def unit(self) -> str:
        return self.crop.unit

    @property
    def is_open(self) -> bool:
        return self.status == "open"

    def __str__(self):
        return f"{self.quantity}{self.unit} {self.crop} wanted in {self.delivery_city}"


class RequestOffer(TimeStampedModel):
    """A farmer's reply to a :class:`DemandRequest`.

    Accepting one creates a real ``Order`` at the offered price, exactly like
    accepting a price offer on a listing.
    """

    STATUS_CHOICES = [
        ("pending", "Pending"),
        ("accepted", "Accepted"),
        ("rejected", "Rejected"),
        ("withdrawn", "Withdrawn"),
    ]

    request = models.ForeignKey(DemandRequest, on_delete=models.CASCADE, related_name="offers")
    farmer = models.ForeignKey(FarmerProfile, on_delete=models.CASCADE, related_name="request_offers")
    quantity = models.DecimalField(max_digits=10, decimal_places=2)
    price_per_unit = models.DecimalField(max_digits=10, decimal_places=2)
    payment_route = models.CharField(
        max_length=10, choices=Order.PAYMENT_ROUTE_CHOICES, default="platform",
        help_text="Seller's choice: take the money via the platform, or take it directly",
    )
    message = models.CharField(max_length=500, blank=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="pending")
    order = models.OneToOneField(
        Order, on_delete=models.SET_NULL, null=True, blank=True, related_name="request_offer"
    )
    responded_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["request", "status"])]
        constraints = [
            models.UniqueConstraint(
                fields=["request", "farmer"],
                condition=models.Q(status="pending"),
                name="unique_pending_offer_per_farmer",
            )
        ]

    @property
    def total_value(self) -> Decimal:
        return (self.quantity * self.price_per_unit).quantize(Decimal("0.01"))

    def __str__(self):
        return f"{self.farmer} offers {self.quantity} x {self.price_per_unit} ({self.status})"


class Conversation(TimeStampedModel):
    """A private thread between one buyer and one farmer.

    A thread is started either by a farmer answering a buyer's demand request
    (``request``) or by a buyer messaging a seller about one of their listings
    (``listing``). Both sides can post either way. Contact details stay hidden
    until a deal is struck.
    """

    request = models.ForeignKey(
        DemandRequest, on_delete=models.CASCADE, related_name="conversations",
        null=True, blank=True,
    )
    listing = models.ForeignKey(
        Listing, on_delete=models.SET_NULL, related_name="conversations",
        null=True, blank=True,
        help_text="Set when the buyer started the thread from a listing instead of the board",
    )
    buyer = models.ForeignKey(BuyerProfile, on_delete=models.CASCADE, related_name="conversations")
    farmer = models.ForeignKey(FarmerProfile, on_delete=models.CASCADE, related_name="conversations")
    order = models.OneToOneField(
        Order, on_delete=models.SET_NULL, null=True, blank=True, related_name="conversation"
    )
    last_message_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-last_message_at", "-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["request", "farmer"], name="unique_conversation_per_request_farmer"
            ),
            # One thread per buyer per listing, so a buyer clicking "Contact
            # seller" twice rejoins the same conversation rather than starting
            # a second one. Postgis/Postgres treat NULLs as distinct, so this
            # leaves the request-based threads above unaffected.
            models.UniqueConstraint(
                fields=["listing", "buyer"], name="unique_conversation_per_listing_buyer"
            ),
        ]

    @property
    def deal_struck(self) -> bool:
        """Contact details unlock once there is a real order behind this thread."""
        return self.order_id is not None

    def __str__(self):
        return f"{self.buyer} ↔ {self.farmer}"


class Message(models.Model):
    """One message in a :class:`Conversation`."""

    conversation = models.ForeignKey(Conversation, on_delete=models.CASCADE, related_name="messages")
    sender = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="messages")
    body = models.TextField(max_length=2000)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at"]

    def __str__(self):
        return f"{self.sender}: {self.body[:40]}"


class PayoutMethod(TimeStampedModel):
    """Where a seller wants their money sent — and, for direct deals, the VPA a
    buyer pays.

    This is payout-destination data only. It is never used to take a payment
    *from* someone, and the account number is masked everywhere it is displayed.
    """

    KIND_CHOICES = [
        ("upi", "UPI ID (VPA)"),
        ("bank", "Bank account"),
    ]

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, null=True, blank=True,
        related_name="payout_methods",
    )
    delivery_partner = models.ForeignKey(
        "DeliveryPartner", on_delete=models.CASCADE, null=True, blank=True,
        related_name="payout_methods",
        help_text="Set instead of a user when this pays a transporter",
    )
    kind = models.CharField(max_length=10, choices=KIND_CHOICES, default="upi")
    upi_id = models.CharField(max_length=100, blank=True)
    account_holder = models.CharField(max_length=150, blank=True)
    account_number = models.CharField(max_length=34, blank=True)
    ifsc = models.CharField(max_length=11, blank=True)
    is_primary = models.BooleanField(default=True)
    is_verified = models.BooleanField(
        default=False, help_text="Set by staff after checking the bank/UPI account name"
    )
    # Who checked and when. A bare boolean on a payment address that buyers are
    # told to trust is not worth much: a badge nobody can date, or attribute,
    # is just an assertion.
    verified_at = models.DateTimeField(null=True, blank=True)
    verified_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
        related_name="verified_payout_methods",
    )

    class Meta:
        ordering = ["-is_primary", "-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["user", "kind"], condition=models.Q(is_primary=True),
                name="one_primary_payout_method_per_kind",
            ),
            models.UniqueConstraint(
                fields=["delivery_partner", "kind"], condition=models.Q(is_primary=True),
                name="one_primary_partner_payout_method_per_kind",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(user__isnull=False, delivery_partner__isnull=True)
                    | models.Q(user__isnull=True, delivery_partner__isnull=False)
                ),
                name="payoutmethod_has_exactly_one_owner",
            ),
        ]

    @property
    def masked_account_number(self) -> str:
        """Never render a full account number back to a user."""
        if not self.account_number:
            return ""
        tail = self.account_number[-4:]
        return f"XXXX{tail}" if len(self.account_number) > 4 else tail

    @property
    def display(self) -> str:
        if self.kind == "upi":
            return self.upi_id
        holder = self.account_holder or "Bank account"
        return f"{holder} {self.masked_account_number} ({self.ifsc})" if self.ifsc else holder

    def __str__(self):
        return f"{self.user} · {self.display}"


# ---------------------------------------------------------------------------
# Payouts — what the platform owes farmers
# ---------------------------------------------------------------------------
class Payout(TimeStampedModel):
    """A settlement statement for one farmer over a period.

    Only **platform-collected** orders appear here. A direct transfer already
    put the money in the farmer's own account and the platform never touched it,
    so there is nothing to settle — that is the deliberate cost of a 0% fee on
    direct deals.

    ``net_amount`` is what the farmer receives: the produce value of every order
    in the statement, plus any manual ``adjustments`` (a refund clawback, a
    goodwill payment). The platform's commission is recorded but *not* netted off
    here — it was already withheld at capture.
    """

    STATUS_CHOICES = [
        ("draft", "Draft"),
        ("approved", "Approved"),
        ("paid", "Paid"),
        ("cancelled", "Cancelled"),
    ]

    farmer = models.ForeignKey(
        FarmerProfile, on_delete=models.PROTECT, null=True, blank=True,
        related_name="payouts",
    )
    delivery_partner = models.ForeignKey(
        "DeliveryPartner", on_delete=models.PROTECT, null=True, blank=True,
        related_name="payouts",
        help_text="Set instead of a farmer when this statement pays a transporter",
    )
    period_start = models.DateField()
    period_end = models.DateField()
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="draft")
    order_count = models.PositiveIntegerField(default=0)
    gross_amount = models.DecimalField(
        max_digits=12, decimal_places=2, default=0,
        help_text="Produce value owed to the farmer",
    )
    commission_amount = models.DecimalField(
        max_digits=12, decimal_places=2, default=0,
        help_text="Platform fee retained from these orders (informational)",
    )
    delivery_amount = models.DecimalField(
        max_digits=12, decimal_places=2, default=0,
        help_text="Delivery charges on these orders — not the farmer's money",
    )
    adjustments = models.DecimalField(
        max_digits=12, decimal_places=2, default=0,
        help_text="Manual corrections; negative values claw money back",
    )
    net_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    method = models.CharField(max_length=10, blank=True, help_text="upi or bank")
    reference = models.CharField(
        max_length=100, blank=True, help_text="Bank or gateway payout reference"
    )
    notes = models.TextField(blank=True)
    approved_at = models.DateTimeField(null=True, blank=True)
    paid_at = models.DateTimeField(null=True, blank=True)
    processed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True,
        on_delete=models.SET_NULL, related_name="processed_payouts",
    )

    class Meta:
        ordering = ["-period_end", "-created_at"]
        indexes = [
            models.Index(fields=["farmer", "status"]),
            models.Index(fields=["status", "period_end"]),
        ]
        constraints = [
            # A statement pays exactly one recipient: a farmer or a transporter,
            # never both and never neither.
            models.CheckConstraint(
                condition=(
                    models.Q(farmer__isnull=False, delivery_partner__isnull=True)
                    | models.Q(farmer__isnull=True, delivery_partner__isnull=False)
                ),
                name="payout_has_exactly_one_recipient",
            )
        ]

    @property
    def is_partner_payout(self) -> bool:
        return self.delivery_partner_id is not None

    @property
    def recipient_name(self) -> str:
        if self.is_partner_payout:
            return str(self.delivery_partner)
        return str(self.farmer) if self.farmer_id else "—"

    @property
    def is_settled(self) -> bool:
        return self.status == "paid"

    def __str__(self):
        return f"Payout #{self.pk} · {self.farmer} · ₹{self.net_amount} ({self.status})"


class PayoutItem(TimeStampedModel):
    """One order inside a payout statement.

    ``order`` is a **OneToOne**, which is the whole integrity story: an order
    physically cannot appear in two payouts, so no amount of clicking "pay" can
    send a farmer the same money twice.
    """

    payout = models.ForeignKey(Payout, on_delete=models.CASCADE, related_name="items")
    order = models.OneToOneField(
        Order, on_delete=models.PROTECT, null=True, blank=True, related_name="payout_item"
    )
    shipment = models.OneToOneField(
        "Shipment", on_delete=models.PROTECT, null=True, blank=True,
        related_name="payout_item",
    )
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    commission = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    delivery_fee = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    settled_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["order_id", "shipment_id"]
        constraints = [
            # Exactly one source of earnings per line, and each source appears at
            # most once across all statements — the no-double-payment guarantee.
            models.CheckConstraint(
                condition=(
                    models.Q(order__isnull=False, shipment__isnull=True)
                    | models.Q(order__isnull=True, shipment__isnull=False)
                ),
                name="payoutitem_has_exactly_one_source",
            )
        ]

    def __str__(self):
        source = f"Order #{self.order_id}" if self.order_id else f"Shipment #{self.shipment_id}"
        return f"{source} → ₹{self.amount}"


# ---------------------------------------------------------------------------
# Settlement operations
# ---------------------------------------------------------------------------
class Reconciliation(TimeStampedModel):
    """A bank statement matched against pending direct-UPI claims.

    Direct UPI payments are confirmed by reading the bank account, so the CSV
    that was uploaded and what it matched is kept as an audit trail. The record
    is created as a dry run (``applied=False``) and only flips to ``applied``
    once staff confirm the preview.
    """

    filename = models.CharField(max_length=255)
    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True,
        on_delete=models.SET_NULL, related_name="reconciliations",
    )
    total_rows = models.PositiveIntegerField(default=0)
    matched_count = models.PositiveIntegerField(default=0)
    unmatched_count = models.PositiveIntegerField(default=0)
    applied = models.BooleanField(default=False)
    applied_at = models.DateTimeField(null=True, blank=True)
    report = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name_plural = "Reconciliations"

    def __str__(self):
        state = "applied" if self.applied else "preview"
        return f"{self.filename} ({self.matched_count}/{self.total_rows} matched, {state})"


# ---------------------------------------------------------------------------
# Logistics
# ---------------------------------------------------------------------------
class DeliveryPartner(TimeStampedModel):
    """A logistics partner (individual driver or a small fleet) fulfilling shipments."""

    VEHICLE_CHOICES = [
        ("bike", "Motorcycle"),
        ("tempo", "Tempo / mini-truck"),
        ("truck", "Truck"),
        ("refrigerated", "Refrigerated van"),
    ]

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True, related_name="delivery_partner",
    )
    name = models.CharField(max_length=150)
    phone_number = models.CharField(max_length=20, unique=True)
    vehicle_type = models.CharField(max_length=20, choices=VEHICLE_CHOICES, default="tempo")
    capacity_kg = models.PositiveIntegerField(default=500)
    is_available = models.BooleanField(default=True)
    current_latitude = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)
    current_longitude = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return f"{self.name} ({self.get_vehicle_type_display()})"


class Shipment(TimeStampedModel):
    """A planned movement of one or more orders, with an optimised route."""

    STATUS_CHOICES = [
        ("planned", "Planned"),
        ("dispatched", "Dispatched"),
        ("completed", "Completed"),
        ("cancelled", "Cancelled"),
    ]

    partner = models.ForeignKey(
        DeliveryPartner, on_delete=models.SET_NULL,
        null=True, blank=True, related_name="shipments",
    )
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="planned")
    planned_distance_km = models.DecimalField(max_digits=8, decimal_places=2, default=0)
    planned_cost = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    optimized_at = models.DateTimeField(null=True, blank=True)
    route_notes = models.TextField(blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"Shipment #{self.id} ({self.status})"


class ShipmentStop(models.Model):
    """An ordered stop on a shipment route (a pickup or a delivery)."""

    KIND_CHOICES = [("pickup", "Pickup"), ("delivery", "Delivery")]

    shipment = models.ForeignKey(Shipment, on_delete=models.CASCADE, related_name="stops")
    order = models.ForeignKey(
        Order, on_delete=models.CASCADE, null=True, blank=True, related_name="shipment_stops"
    )
    kind = models.CharField(max_length=10, choices=KIND_CHOICES, default="delivery")
    sequence = models.PositiveIntegerField(default=0)
    label = models.CharField(max_length=200)
    latitude = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)
    longitude = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)
    status = models.CharField(max_length=20, default="pending")
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["sequence"]

    def __str__(self):
        return f"{self.sequence}. {self.label} ({self.kind})"

    @property
    def is_delivery(self) -> bool:
        return self.kind == "delivery"

    @property
    def is_closed(self) -> bool:
        return self.completed_at is not None


class DeliveryProof(TimeStampedModel):
    """Evidence that produce actually reached the buyer.

    Both sides of the delivery have something to stand on:

    * The **transporter** cannot claim a delivery charge for a drop nobody
      witnessed. The buyer's one-time code is the receipt.
    * The **buyer** gets a timestamp, a location and an image, so "it never
      arrived" is a dispute with evidence rather than an argument from memory.

    Three independent pieces of evidence, any one of which is enough on its own,
    because real drivers have patchy signal and no camera:

    ``otp``
        A short code shown to the buyer and typed in by the driver. The strongest
        proof available — only the person receiving the goods can produce it.
    ``photo``
        The load at the door. Cheap to capture, needs ``USE_S3`` to outlive a
        redeploy (see :mod:`marketplace.services.delivery`).
    ``signature``
        Vector strokes stored as an SVG path in a text column. No image file, so
        it costs nothing to keep and cannot rot.

    The code is stored **hashed**, like the email verification token: a leaked
    database must not let anyone confirm a delivery they did not make.
    """

    STATUS_CHOICES = [
        ("recorded", "Recorded, awaiting buyer"),
        ("confirmed", "Confirmed by both sides"),
        ("disputed", "Disputed by the buyer"),
    ]

    stop = models.OneToOneField(
        ShipmentStop, on_delete=models.CASCADE, related_name="proof"
    )
    order = models.ForeignKey(
        Order, on_delete=models.PROTECT, related_name="delivery_proofs"
    )
    shipment = models.ForeignKey(
        Shipment, on_delete=models.PROTECT, related_name="proofs"
    )

    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="recorded")

    otp_hash = models.CharField(max_length=64, blank=True, db_index=True)
    otp_sent_at = models.DateTimeField(null=True, blank=True)
    otp_confirmed_at = models.DateTimeField(null=True, blank=True)
    otp_attempts = models.PositiveIntegerField(default=0)

    photo = models.ImageField(upload_to="proofs/%Y/%m", blank=True, null=True)
    signature = models.TextField(
        blank=True, help_text="SVG path data captured from the driver's finger"
    )
    notes = models.CharField(max_length=500, blank=True)

    latitude = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)
    longitude = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)

    recorded_by = models.ForeignKey(
        "DeliveryPartner", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="recorded_proofs",
    )
    recorded_by_user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="recorded_proofs",
    )
    confirmed_by_buyer = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="confirmed_proofs",
        help_text="The buyer who accepted the delivery",
    )
    dispute_reason = models.CharField(max_length=300, blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["order", "status"]),
            models.Index(fields=["shipment", "status"]),
        ]

    # -- evidence ---------------------------------------------------------
    @property
    def has_code_issued(self) -> bool:
        """A code has been minted for this stop and not yet superseded."""
        return bool(self.otp_hash)

    @property
    def has_otp(self) -> bool:
        """The buyer actually read the code back to the driver.

        An *issued* code proves nothing — it is just a number sitting in the
        database. Only a verified one is evidence.
        """
        return self.otp_confirmed_at is not None

    @property
    def has_photo(self) -> bool:
        return bool(self.photo)

    @property
    def has_signature(self) -> bool:
        return bool(self.signature.strip())

    @property
    def evidence_count(self) -> int:
        """How many independent kinds of evidence this proof carries."""
        return sum((self.has_otp, self.has_photo, self.has_signature))

    @property
    def is_confirmed(self) -> bool:
        return self.status == "confirmed"

    @property
    def is_disputed(self) -> bool:
        return self.status == "disputed"

    def __str__(self):
        return f"Proof for stop #{self.stop_id} ({self.status})"


# ---------------------------------------------------------------------------
# Intelligence
# ---------------------------------------------------------------------------
class DemandForecast(models.Model):
    """A stored demand/price prediction for a crop in a region."""

    METHOD_CHOICES = [("llm", "LLM"), ("heuristic", "Heuristic")]

    crop = models.ForeignKey(Crop, on_delete=models.CASCADE, related_name="forecasts")
    region = models.CharField(max_length=150, help_text="District or city the forecast applies to")
    horizon_days = models.PositiveSmallIntegerField(default=7)
    predicted_quantity = models.DecimalField(
        max_digits=12, decimal_places=2, null=True, blank=True,
        help_text=f"Expected demand over the horizon, in the crop's unit",
    )
    predicted_price_per_unit = models.DecimalField(
        max_digits=10, decimal_places=2, null=True, blank=True
    )
    confidence = models.PositiveSmallIntegerField(default=0, help_text="0-100")
    method = models.CharField(max_length=20, choices=METHOD_CHOICES, default="heuristic")
    rationale = models.TextField(blank=True)
    raw_response = models.JSONField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["crop", "region", "-created_at"]),
        ]

    def __str__(self):
        return f"{self.crop} @ {self.region} (+{self.horizon_days}d)"


# ---------------------------------------------------------------------------
# Operations
# ---------------------------------------------------------------------------
class ServiceCheck(models.Model):
    """One result from the health watchdog, kept as a short history.

    Written every few minutes by the ``check_services`` command. The point is
    not the individual rows but the *transitions*: a site that is merely
    sleeping looks identical to one that is broken until you compare two runs.

    History is pruned to ``KEEP_ROWS`` in :meth:`record` so an every-5-minute
    cron does not quietly grow the table by a million rows a year.
    """

    KEEP_ROWS = 500

    SEVERITY = [
        ("ok", "OK"),
        ("warn", "Warning"),
        ("fail", "Failing"),
    ]

    name = models.CharField(max_length=50, db_index=True)
    severity = models.CharField(max_length=8, choices=SEVERITY, default="ok")
    detail = models.CharField(max_length=255, blank=True)
    #: True only for the row that changed state, so a repeat failure is not
    #: mailed twice.
    is_transition = models.BooleanField(default=False)
    checked_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-checked_at"]
        verbose_name_plural = "Service checks"
        indexes = [
            models.Index(fields=["name", "-checked_at"]),
        ]

    def __str__(self):
        return f"{self.name}: {self.severity}"

    @property
    def ok(self) -> bool:
        return self.severity == "ok"

    @classmethod
    def record(cls, results: list, *, only_changed: bool = True) -> list:
        """Persist ``results``; return the rows that represent a new state.

        A check that stays broken should not mail you every five minutes. Only
        the transition — ok to failing, or back again — is worth an alert.

        A first-ever result is deliberately *not* a transition: on a fresh
        deploy every check is "new", and treating that as news produces one
        alert storm instead of silence. ``only_changed=False`` forces every row
        to count, which is what the tests use.
        """
        saved = []
        for result in results:
            name = result["name"]
            severity = result.get("severity", "ok")
            previous = cls.objects.filter(name=name).order_by("-checked_at").first()

            if only_changed:
                changed = previous is not None and previous.severity != severity
            else:
                changed = True

            saved.append(
                cls.objects.create(
                    name=name,
                    severity=severity,
                    detail=(result.get("detail") or "")[:255],
                    is_transition=changed,
                )
            )

        cls.prune()
        return saved

    @classmethod
    def prune(cls, keep: int = KEEP_ROWS) -> int:
        """Drop all but the ``keep`` newest rows."""
        total = cls.objects.count()
        if total <= keep:
            return 0
        doomed = cls.objects.order_by("-checked_at").values_list("pk", flat=True)[keep:]
        deleted, _ = cls.objects.filter(pk__in=list(doomed)).delete()
        return deleted

    @classmethod
    def latest(cls) -> dict:
        """Newest severity per check name, for a status page."""
        newest = {}
        for row in cls.objects.order_by("-checked_at"):
            newest.setdefault(row.name, row)
        return newest

    @classmethod
    def overall(cls) -> str:
        rows = list(cls.latest().values())
        if any(row.severity == "fail" for row in rows):
            return "fail"
        if any(row.severity == "warn" for row in rows):
            return "warn"
        return "ok" if rows else "unknown"

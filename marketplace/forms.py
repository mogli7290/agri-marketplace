"""Forms for the marketplace application."""

import re
from decimal import Decimal

from django import forms
from django.contrib.auth import get_user_model
from django.contrib.auth.forms import (
    AuthenticationForm,
    PasswordResetForm as DjangoPasswordResetForm,
    SetPasswordForm as DjangoSetPasswordForm,
    UserCreationForm,
)
from django.core.exceptions import ValidationError
from django.core.validators import FileExtensionValidator

from marketplace.models import (
    BuyerProfile,
    Crop,
    DemandRequest,
    FarmerProfile,
    Listing,
    Message,
    Order,
    PriceOffer,
    PayoutMethod,
)
from marketplace.services import deals as deals_service
from marketplace.services.upi import is_valid_vpa as upi_valid_vpa

User = get_user_model()


class BootstrapFormMixin:
    """Apply Bootstrap classes to every widget without repeating it field by field."""

    def _apply_bootstrap(self):
        for field in self.fields.values():
            widget = field.widget
            if isinstance(widget, (forms.CheckboxInput, forms.RadioSelect)):
                continue
            css = widget.attrs.get("class", "")
            if isinstance(widget, (forms.Select, forms.SelectMultiple)):
                widget.attrs["class"] = f"{css} form-select".strip()
            else:
                widget.attrs["class"] = f"{css} form-control".strip()


class RegistrationForm(BootstrapFormMixin, UserCreationForm):
    """Registration with farmer/buyer role selection and role-specific fields."""

    USER_TYPE_CHOICES = [
        ("farmer", "Farmer / FPO"),
        ("buyer", "Consumer / Bulk Buyer"),
    ]

    user_type = forms.ChoiceField(choices=USER_TYPE_CHOICES, widget=forms.RadioSelect)
    full_name = forms.CharField(max_length=150, label="Full name")
    phone_number = forms.CharField(max_length=20, label="Phone number")
    email = forms.EmailField(required=True)

    # Farmer fields.
    is_fpo = forms.BooleanField(required=False, label="This account represents an FPO")
    village = forms.CharField(max_length=150, required=False)
    district = forms.CharField(max_length=150, required=False)
    state = forms.CharField(max_length=150, required=False)

    # Buyer fields.
    buyer_type = forms.ChoiceField(
        choices=BuyerProfile.BUYER_TYPE_CHOICES, required=False
    )
    business_name = forms.CharField(max_length=150, required=False, label="Business name (optional)")
    city = forms.CharField(max_length=150, required=False)
    address = forms.CharField(widget=forms.Textarea(attrs={"rows": 3}), required=False)

    class Meta:
        model = User
        fields = ["username", "email", "password1", "password2"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._apply_bootstrap()

    def clean_email(self):
        email = self.cleaned_data["email"].strip().lower()
        if User.objects.filter(email__iexact=email).exists():
            raise ValidationError("An account with this email already exists.")
        return email

    def clean_phone_number(self):
        phone = self.cleaned_data["phone_number"].strip()
        if FarmerProfile.objects.filter(phone_number=phone).exists() or BuyerProfile.objects.filter(
            phone_number=phone
        ).exists():
            raise ValidationError("An account with this phone number already exists.")
        return phone

    def clean(self):
        cleaned = super().clean()
        role = cleaned.get("user_type")
        if role == "farmer":
            for field in ("village", "district", "state"):
                if not cleaned.get(field):
                    self.add_error(field, "This field is required for farmers.")
        elif role == "buyer":
            if not cleaned.get("buyer_type"):
                self.add_error("buyer_type", "Please choose a buyer type.")
            if not cleaned.get("city"):
                self.add_error("city", "City is required for buyers.")
        return cleaned


class LoginForm(BootstrapFormMixin, AuthenticationForm):
    """Login by username or email."""

    username = forms.CharField(label="Username or email")
    password = forms.CharField(widget=forms.PasswordInput, label="Password")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._apply_bootstrap()


class EmailResendForm(BootstrapFormMixin, forms.Form):
    """Request a fresh email-confirmation link.

    Deliberately says nothing about whether the address exists — that would turn
    the form into an account-enumeration oracle.
    """

    email = forms.EmailField(label="Email address", max_length=254)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._apply_bootstrap()


class ListingForm(BootstrapFormMixin, forms.ModelForm):
    """Create or edit a listing."""

    #: Uploads land on ephemeral storage on the free tier, so keep them small
    #: and to image formats only.
    PHOTO_MAX_BYTES = 5 * 1024 * 1024

    class Meta:
        model = Listing
        fields = [
            "crop",
            "quantity_available",
            "quality_grade",
            "price_per_unit",
            "harvest_date",
            "is_organic",
            "description",
            "photo",
        ]
        widgets = {
            "harvest_date": forms.DateInput(attrs={"type": "date"}),
            "quantity_available": forms.NumberInput(attrs={"step": "0.01", "min": "0.01"}),
            "price_per_unit": forms.NumberInput(attrs={"step": "0.01", "min": "0"}),
            "description": forms.Textarea(attrs={"rows": 3}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["crop"].queryset = Crop.objects.all()
        self.fields["crop"].empty_label = "Select a crop"
        # Plain-language help. Farmers are not assumed to know what a listing
        # field means in marketplace terms, and the form template renders this
        # text under each control — visible on a phone, unlike a tooltip.
        self.fields["quantity_available"].help_text = (
            "How much you can sell right now, in the unit you choose (usually kg)."
        )
        self.fields["quality_grade"].help_text = (
            "A is the best quality, C the lowest. Buyers search and filter by this, "
            "so be honest — it decides what you are offered."
        )
        self.fields["price_per_unit"].help_text = (
            "What one unit sells for. Buyers see this next to your quantity."
        )
        self.fields["harvest_date"].help_text = (
            "The day it was picked or packed. Buyers judge freshness by this."
        )
        self.fields["description"].help_text = (
            "Optional. Say how it was grown, how fresh it is, or how much you can "
            "supply at once."
        )
        self.fields["photo"].help_text = (
            "Optional, under 5 MB. If you skip it, we show a picture of the crop instead."
        )
        self.fields["photo"].validators = [
            FileExtensionValidator(["jpg", "jpeg", "png", "webp"])
        ]
        self._apply_bootstrap()

    def clean_photo(self):
        photo = self.cleaned_data.get("photo")
        if photo and getattr(photo, "size", 0) > self.PHOTO_MAX_BYTES:
            raise ValidationError("Photo must be smaller than 5 MB.")
        return photo

    def clean_quantity_available(self):
        quantity = self.cleaned_data["quantity_available"]
        if quantity <= 0:
            raise ValidationError("Quantity must be greater than zero.")
        return quantity

    def clean_price_per_unit(self):
        price = self.cleaned_data["price_per_unit"]
        if price <= 0:
            raise ValidationError("Price must be greater than zero.")
        return price


class OrderForm(BootstrapFormMixin, forms.ModelForm):
    """Place an order at the farmer's listed price.

    The buyer only chooses a quantity — price is not the buyer's to set. To
    negotiate, they submit an :class:`OfferForm` instead and wait for approval.
    """

    class Meta:
        model = Order
        fields = ["quantity_ordered"]
        widgets = {
            "quantity_ordered": forms.NumberInput(attrs={"step": "0.01", "min": "0.01"}),
        }

    def __init__(self, *args, **kwargs):
        self.listing = kwargs.pop("listing", None)
        super().__init__(*args, **kwargs)
        self._apply_bootstrap()
        if self.listing:
            self.fields["quantity_ordered"].widget.attrs["max"] = str(
                self.listing.quantity_available
            )
            self.fields["quantity_ordered"].help_text = (
                f"₹{self.listing.price_per_unit}/{self.listing.crop.unit} · "
                f"available: {self.listing.quantity_available} {self.listing.crop.unit}"
            )

    def clean_quantity_ordered(self):
        quantity = self.cleaned_data["quantity_ordered"]
        if quantity <= 0:
            raise ValidationError("Quantity must be greater than zero.")
        if self.listing and quantity > self.listing.quantity_available:
            raise ValidationError(
                f"Only {self.listing.quantity_available} {self.listing.crop.unit} available."
            )
        return quantity


class OfferForm(BootstrapFormMixin, forms.ModelForm):
    """A buyer's price offer, sent to the farmer for approval."""

    class Meta:
        model = PriceOffer
        fields = ["quantity_requested", "proposed_price", "message"]
        widgets = {
            "quantity_requested": forms.NumberInput(attrs={"step": "0.01", "min": "0.01"}),
            "proposed_price": forms.NumberInput(attrs={"step": "0.01", "min": "0.01"}),
            "message": forms.Textarea(attrs={"rows": 2, "placeholder": "Optional note to the farmer"}),
        }

    def __init__(self, *args, **kwargs):
        self.listing = kwargs.pop("listing", None)
        super().__init__(*args, **kwargs)
        self._apply_bootstrap()
        if self.listing:
            self.fields["quantity_requested"].widget.attrs["max"] = str(
                self.listing.quantity_available
            )
            self.fields["proposed_price"].initial = self.listing.price_per_unit
            self.fields["proposed_price"].help_text = (
                f"Listed at ₹{self.listing.price_per_unit}/{self.listing.crop.unit}"
            )

    def clean_quantity_requested(self):
        quantity = self.cleaned_data["quantity_requested"]
        if quantity <= 0:
            raise ValidationError("Quantity must be greater than zero.")
        if self.listing and quantity > self.listing.quantity_available:
            raise ValidationError(
                f"Only {self.listing.quantity_available} {self.listing.crop.unit} available."
            )
        return quantity

    def clean_proposed_price(self):
        price = self.cleaned_data["proposed_price"]
        if price <= 0:
            raise ValidationError("Offered price must be greater than zero.")
        return price


class DemandRequestForm(BootstrapFormMixin, forms.ModelForm):
    """A buyer posting "I want this much of this"."""

    class Meta:
        model = DemandRequest
        fields = ["crop", "quantity", "target_price", "delivery_city", "needed_by", "notes"]
        widgets = {
            "quantity": forms.NumberInput(attrs={"step": "0.01", "min": "0.01"}),
            "target_price": forms.NumberInput(attrs={"step": "0.01", "min": "0.01"}),
            "needed_by": forms.DateInput(attrs={"type": "date"}),
            "notes": forms.Textarea(
                attrs={"rows": 3, "placeholder": "Grade, organic, packing preference…"}
            ),
            "target_price": forms.NumberInput(
                attrs={"step": "0.01", "min": "0.01", "placeholder": "Optional"}
            ),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["crop"].queryset = Crop.objects.all()
        self.fields["crop"].empty_label = "Select a crop"
        self.fields["target_price"].required = False
        self.fields["needed_by"].required = False
        self._apply_bootstrap()

    def clean_quantity(self):
        quantity = self.cleaned_data["quantity"]
        if quantity <= 0:
            raise ValidationError("Quantity must be greater than zero.")
        return quantity

    def clean(self):
        cleaned = super().clean()
        target = cleaned.get("target_price")
        if target is not None and target <= 0:
            self.add_error("target_price", "Target price must be greater than zero.")
        return cleaned


class RequestOfferForm(BootstrapFormMixin, forms.Form):
    """A farmer replying to a demand request."""

    quantity = forms.DecimalField(
        min_value=Decimal("0.01"), max_digits=10, decimal_places=2,
        widget=forms.NumberInput(attrs={"step": "0.01", "min": "0.01"}),
    )
    price_per_unit = forms.DecimalField(
        min_value=Decimal("0.01"), max_digits=10, decimal_places=2,
        widget=forms.NumberInput(attrs={"step": "0.01", "min": "0.01"}),
        label="Your price per unit",
    )
    payment_route = forms.ChoiceField(
        choices=[
            ("platform", "Through the platform (I pay a small fee, you get settled)"),
            ("direct", "Direct to my UPI ID (0% fee, you pay me directly)"),
        ],
        widget=forms.RadioSelect,
        help_text="Direct transfers carry no platform fee — you keep 100%.",
    )
    message = forms.CharField(
        required=False, max_length=500,
        widget=forms.Textarea(attrs={"rows": 2, "placeholder": "Tell the buyer what you can offer"}),
    )

    def __init__(self, *args, **kwargs):
        can_direct = kwargs.pop("can_direct", False)
        super().__init__(*args, **kwargs)
        if not can_direct:
            # No UPI ID on file: the direct route would have nowhere to send money.
            self.fields["payment_route"].choices = [
                ("platform", "Through the platform (I pay a small fee, you get settled)")
            ]
            self.fields["payment_route"].initial = "platform"
        self._apply_bootstrap()


class MessageForm(BootstrapFormMixin, forms.ModelForm):
    class Meta:
        model = Message
        fields = ["body"]
        widgets = {
            "body": forms.Textarea(
                attrs={"rows": 3, "placeholder": "Write a message…", "maxlength": 2000}
            )
        }
        labels = {"body": ""}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._apply_bootstrap()


class PayoutMethodForm(BootstrapFormMixin, forms.Form):
    """Where this seller wants to be paid. UPI ID or bank account."""

    kind = forms.ChoiceField(choices=PayoutMethod.KIND_CHOICES, widget=forms.RadioSelect)
    upi_id = forms.CharField(
        required=False, max_length=100, label="UPI ID",
        widget=forms.TextInput(attrs={"placeholder": "yourname@okaxis"}),
        help_text="Buyers paying you directly will scan this.",
    )
    account_holder = forms.CharField(required=False, max_length=150, label="Account holder name")
    account_number = forms.CharField(required=False, max_length=34, label="Account number")
    ifsc = forms.CharField(required=False, max_length=11, label="IFSC code")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._apply_bootstrap()

    def clean(self):
        """Shape checks only.

        The authoritative validation (and the write) happens in
        ``deals_service.save_payout_method``, so the web form and any future API
        endpoint cannot drift apart.
        """
        cleaned = super().clean()
        kind = cleaned.get("kind")

        if kind == "upi":
            upi_id = (cleaned.get("upi_id") or "").strip()
            if not upi_id:
                self.add_error("upi_id", "Enter the UPI ID buyers should pay.")
            elif not upi_valid_vpa(upi_id):
                self.add_error("upi_id", "That does not look like a UPI ID (name@bank).")
        elif kind == "bank":
            if not (cleaned.get("account_holder") or "").strip():
                self.add_error("account_holder", "Enter the account holder's name.")
            number = re.sub(r"\s+", "", cleaned.get("account_number") or "")
            if not (number.isdigit() and 6 <= len(number) <= 18):
                self.add_error("account_number", "Enter a valid account number (6-18 digits).")
            ifsc = (cleaned.get("ifsc") or "").strip().upper()
            if not (len(ifsc) == 11 and ifsc[:4].isalpha() and ifsc[4:].isalnum()):
                self.add_error("ifsc", "Enter a valid IFSC code, e.g. HDFC0001234.")
        return cleaned


class FarmerProfileForm(BootstrapFormMixin, forms.ModelForm):
    class Meta:
        model = FarmerProfile
        fields = [
            "full_name", "phone_number", "village", "district", "state",
            "preferred_language", "latitude", "longitude", "is_fpo",
        ]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._apply_bootstrap()


class BuyerProfileForm(BootstrapFormMixin, forms.ModelForm):
    class Meta:
        model = BuyerProfile
        fields = [
            "buyer_type", "business_name", "phone_number", "city",
            "address", "latitude", "longitude",
        ]
        widgets = {"address": forms.Textarea(attrs={"rows": 3})}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._apply_bootstrap()


class DeliveryProofForm(BootstrapFormMixin, forms.Form):
    """What a driver captures at the door.

    Shape checks only — the authoritative validation (and the write) lives in
    ``delivery_service.record_proof``, so the web form and any future API
    endpoint cannot drift apart.
    """

    code = forms.CharField(
        max_length=6,
        label="Delivery code",
        widget=forms.TextInput(
            attrs={
                "placeholder": "6-digit code",
                "inputmode": "numeric",
                "autocomplete": "one-time-code",
                "class": "form-control text-center",
                "style": "font-size:1.5rem;letter-spacing:6px;max-width:14rem;",
            }
        ),
        help_text="Ask the buyer for the code we emailed them.",
    )
    photo = forms.FileField(
        required=False,
        label="Photo of the delivery",
        help_text="Optional. Keep it under 5 MB.",
    )
    signature = forms.CharField(
        required=False,
        widget=forms.HiddenInput(),
        help_text="Drawn in the box above.",
    )
    notes = forms.CharField(
        required=False, max_length=500, label="Note",
        widget=forms.Textarea(attrs={"rows": 2, "placeholder": "Left with the gatekeeper, etc."}),
    )
    latitude = forms.DecimalField(
        required=False, max_digits=9, decimal_places=6, widget=forms.HiddenInput()
    )
    longitude = forms.DecimalField(
        required=False, max_digits=9, decimal_places=6, widget=forms.HiddenInput()
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._apply_bootstrap()

    def clean_code(self):
        code = (self.cleaned_data.get("code") or "").strip().replace(" ", "")
        if not code.isdigit() or len(code) != 6:
            raise forms.ValidationError("The delivery code is six digits.")
        return code

    def clean_photo(self):
        photo = self.cleaned_data.get("photo")
        if photo is None:
            return None
        if photo.size > 5 * 1024 * 1024:
            raise forms.ValidationError("That photo is too large. Keep it under 5 MB.")
        if photo.content_type not in {"image/jpeg", "image/png", "image/webp"}:
            raise forms.ValidationError("Upload a JPEG, PNG or WebP image.")
        return photo


class DeliveryDisputeForm(BootstrapFormMixin, forms.Form):
    """The buyer saying "this did not arrive"."""

    reason = forms.CharField(
        max_length=300,
        label="What went wrong?",
        widget=forms.Textarea(
            attrs={"rows": 3, "placeholder": "Nothing arrived, or the quantity was short…"}
        ),
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._apply_bootstrap()


class PasswordResetForm(BootstrapFormMixin, DjangoPasswordResetForm):
    """Ask for the account email so a reset link can be sent.

    The form validates the address but deliberately does **not** reveal whether
    an account exists — the view answers identically either way, so this page
    cannot be used to enumerate users.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["email"].label = "Email address"
        self.fields["email"].widget.attrs["placeholder"] = "you@example.com"
        self._apply_bootstrap()


class SetPasswordForm(BootstrapFormMixin, DjangoSetPasswordForm):
    """Choose a new password from a valid reset link."""

    def __init__(self, *args, **kwargs):
        user = kwargs.pop("user", None)
        super().__init__(user, *args, **kwargs)
        self._apply_bootstrap()

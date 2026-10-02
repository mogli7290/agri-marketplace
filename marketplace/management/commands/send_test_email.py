"""Send a real test email through the configured backend.

Tells you exactly what is and is not wired up, so a silent "no mail arrives"
takes one command to diagnose instead of an afternoon.

    python manage.py send_test_email --to you@example.com
    python manage.py send_test_email --to you@example.com --check-only

Nothing but verification-style templates are ever sent; there is no risk of
mailing real users.
"""

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = "Send a test email to verify the SMTP configuration."

    def add_arguments(self, parser):
        parser.add_argument("--to", required=True, help="Recipient address")
        parser.add_argument(
            "--check-only",
            action="store_true",
            help="Report the configuration without sending anything",
        )

    def handle(self, *args, **options):
        backend = settings.EMAIL_BACKEND
        is_console = backend.endswith("console.EmailBackend")
        is_smtp = backend.endswith("smtp.EmailBackend")
        is_brevo = "BrevoEmailBackend" in backend

        self.stdout.write("")
        self.stdout.write("Email configuration")
        self.stdout.write("  backend : " + backend)
        if is_smtp:
            self.stdout.write("  host    : " + (settings.EMAIL_HOST or "(not set)"))
            self.stdout.write("  port    : " + str(settings.EMAIL_PORT))
            self.stdout.write("  user    : " + (settings.EMAIL_HOST_USER or "(not set)"))
        if is_brevo:
            key = getattr(settings, "BREVO_API_KEY", "") or settings.EMAIL_HOST_PASSWORD
            self.stdout.write("  api key : " + (f"{key[:11]}…({len(key)} chars)" if key else "(not set)"))
        self.stdout.write("  from    : " + settings.DEFAULT_FROM_EMAIL)
        self.stdout.write("  site url: " + (settings.SITE_URL or "(not set)"))
        self.stdout.write("")

        self._check_site_url()

        if is_console:
            self.stdout.write(
                self.style.WARNING(
                    "Console backend: the message will be PRINTED here, not delivered.\n"
                    "Set DJANGO_EMAIL_BACKEND (and the matching credentials) to send\n"
                    "real mail. Run this command again afterwards to confirm."
                )
            )

        if is_smtp and not settings.EMAIL_HOST:
            raise CommandError(
                "EMAIL_HOST is empty, so the SMTP backend has nowhere to connect."
            )
        if is_brevo and not (getattr(settings, "BREVO_API_KEY", "") or settings.EMAIL_HOST_PASSWORD):
            raise CommandError("BREVO_API_KEY is empty, so Brevo cannot authenticate.")

        if options["check_only"]:
            self.stdout.write(self.style.SUCCESS("Configuration looks usable."))
            self.stdout.write("")
            return

        from marketplace.services import messaging
        from marketplace.services.accounts import site_base_url

        # A test email is exactly how you notice a bad host, so it must not fall
        # back to a hardcoded loopback: that link is dead on a phone.
        base_url = site_base_url() or "http://127.0.0.1:8000"
        if not settings.SITE_URL:
            self.stdout.write(
                self.style.WARNING(
                    "SITE_URL is unset — links will be built from the fallback above."
                )
            )
            self.stdout.write("")

        delivered = messaging.send_email(
            to=options["to"],
            subject=f"{settings.SITE_NAME} email test",
            template="verify_email",
            context={
                "user": type("U", (), {"username": "test", "first_name": "there",
                                       "email": options["to"]})(),
                "verification_url": f"{base_url}/verify-email/test-token/",
                "expiry_hours": max(1, settings.EMAIL_VERIFICATION_MAX_AGE // 3600),
            },
        )
        self._preview_samples(options["to"])

        self.stdout.write("")
        if delivered and not is_console:
            self.stdout.write(self.style.SUCCESS(f"Accepted by the provider → {options['to']}"))
            self.stdout.write("Check the inbox and the spam folder.")
        elif delivered:
            self.stdout.write("Printed above (console backend).")
        else:
            self.stdout.write(
                self.style.ERROR("The backend refused the message — see the log above.")
            )
        self.stdout.write("")

    def _check_site_url(self):
        """Warn when links in mail cannot possibly be opened.

        A reset or verification link that points at the wrong port or at
        loopback is a link that fails on the phone, long after the mail is sent
        and with no trace on the server. Say so here, loudly.
        """
        from urllib.parse import urlparse

        from marketplace.services.accounts import site_base_url

        base = site_base_url()
        if not base:
            self.stdout.write(
                self.style.ERROR(
                    "SITE_URL is unset: links will be built from the request.\n"
                    "Set SITE_URL in .env to the address you actually open the site at."
                )
            )
            self.stdout.write("")
            return

        parsed = urlparse(base)
        if parsed.scheme not in ("http", "https"):
            self.stdout.write(
                self.style.ERROR(f"SITE_URL has a {parsed.scheme!r} scheme: {base}")
            )
            self.stdout.write("")
        if parsed.hostname in ("localhost", "127.0.0.1", "0.0.0.0", "::1"):
            self.stdout.write(
                self.style.WARNING(
                    f"SITE_URL is a loopback address ({base}).\n"
                    "Links will only work on this machine. If you read the mail on a\n"
                    "phone, use this machine's LAN address instead, e.g.\n"
                    "  SITE_URL=http://<your-lan-ip>:8001"
                )
            )
            self.stdout.write("")

    def _preview_samples(self, address):
        """Send one of each order notification so the wording can be eyeballed."""
        from decimal import Decimal

        from django.utils import timezone

        from marketplace.services import messaging
        from marketplace.services.accounts import site_base_url

        base = site_base_url() or "http://127.0.0.1:8000"
        today = timezone.localdate()
        samples = {
            "order_placed": {
                "order_id": 1, "buyer_name": "Sahyadri Traders", "crop_name": "Tomato",
                "quantity": Decimal("25.00"), "unit": "kg",
                "price_per_unit": Decimal("40.00"), "amount": Decimal("1000.00"),
                "order_url": f"{base}/orders/1/",
            },
            "payment_confirmed": {
                "order_id": 1, "amount": Decimal("1020.00"), "farmer_name": "Ravindra Patil",
                "crop_name": "Tomato", "quantity": Decimal("25.00"), "unit": "kg",
                "order_url": f"{base}/orders/1/",
            },
            "offer_received": {
                "request_id": 7, "crop_name": "Onion", "quantity": Decimal("100.00"),
                "unit": "kg", "price_per_unit": Decimal("28.00"),
                "farmer_name": "Ravindra Patil", "farmer_village": "Shirur",
                "payment_route": "platform", "request_url": f"{base}/requests/7/",
            },
            "offer_accepted": {
                "order_id": 1, "buyer_name": "Sahyadri Traders", "crop_name": "Onion",
                "quantity": Decimal("100.00"), "unit": "kg",
                "price_per_unit": Decimal("28.00"), "order_url": f"{base}/orders/1/",
            },
            "order_dispatched": {
                "order_id": 1, "crop_name": "Tomato", "quantity": Decimal("25.00"),
                "unit": "kg", "farmer_name": "Ravindra Patil", "pickup_hub": "Shirur",
                "order_url": f"{base}/orders/1/",
            },
            "order_delivered": {
                "order_id": 1, "crop_name": "Tomato", "quantity": Decimal("25.00"),
                "unit": "kg", "farmer_name": "Ravindra Patil",
                "order_url": f"{base}/orders/1/",
            },
            "payout_settled": {
                "payout_id": 3, "recipient": "Ravindra Patil",
                "amount": Decimal("8420.00"), "period_start": today,
                "period_end": today, "reference": "UTR/NEFT 12345",
                "items": [{"label": "Order #1", "amount": Decimal("8420.00")}],
                "earnings_url": f"{base}/earnings/",
            },
            "delivery_code": {
                "order_id": 1, "code": "482913", "expires_hours": 24,
                "crop_name": "Tomato", "quantity": Decimal("25.00"), "unit": "kg",
                "order_url": f"{base}/orders/1/",
            },
            "delivery_recorded": {
                "order_id": 1, "crop_name": "Tomato", "quantity": Decimal("25.00"),
                "unit": "kg", "farmer_name": "Ravindra Patil",
                "evidence": ["delivery code", "signature"],
                "notes": "Left with the gatekeeper",
                "photo_url": "", "order_url": f"{base}/orders/1/",
            },
            "delivery_confirmed": {
                "order_id": 1, "crop_name": "Tomato", "quantity": Decimal("25.00"),
                "unit": "kg", "farmer_name": "Ravindra Patil",
                "evidence": ["delivery code"], "shipment_url": f"{base}/shipments/1/",
            },
            "delivery_disputed": {
                "order_id": 1, "crop_name": "Tomato", "quantity": Decimal("25.00"),
                "unit": "kg", "farmer_name": "Ravindra Patil",
                "reason": "Only 20 kg arrived and half of it was spoiled.",
                "shipment_url": f"{base}/shipments/1/",
            },
        }

        self.stdout.write("")
        self.stdout.write("Notification previews")
        from marketplace.services import notifications

        for event, context in samples.items():
            subject = notifications.SUBJECTS[event].format(**context, site_name=settings.SITE_NAME)
            ok = messaging.send_email(address, subject, event, context)
            self.stdout.write(
                f"  {'sent  ' if ok else 'FAILED'}  {event:<20} {subject}"
            )
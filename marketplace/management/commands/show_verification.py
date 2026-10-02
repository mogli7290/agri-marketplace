"""Print a pending email-verification link.

Locally the console email backend prints the whole message to the terminal,
which is easy to miss in a busy log. This pulls out just the link:

    python manage.py show_verification
    python manage.py show_verification --username demo_farmer
    python manage.py show_verification --latest-verified
"""

from django.core.management.base import BaseCommand, CommandError
from django.urls import reverse

from marketplace.models import EmailVerification
from marketplace.services import accounts as accounts_service


class Command(BaseCommand):
    help = "Show the email-verification link for a pending (or last verified) account."

    def add_arguments(self, parser):
        parser.add_argument("--username", help="Only this user's record")
        parser.add_argument(
            "--latest-verified",
            action="store_true",
            help="Show the most recently verified account instead of pending ones",
        )
        parser.add_argument(
            "--base-url",
            default=accounts_service.site_base_url(),
            help="Host to build the link against (default: SITE_URL, or %(default)s)",
        )

    def handle(self, *args, **options):
        records = EmailVerification.objects.select_related("user")

        if options["username"]:
            records = records.filter(user__username=options["username"])
            if not records.exists():
                raise CommandError(f"No account named {options['username']!r}.")

        if options["latest_verified"]:
            record = records.filter(verified_at__isnull=False).order_by("-verified_at").first()
        else:
            record = records.filter(verified_at__isnull=True).order_by("-created_at").first()

        if record is None:
            self.stdout.write(
                self.style.WARNING(
                    "No matching verification. Register at /register/ to create one."
                )
            )
            return

        # A verified account cannot reuse its token, and a token whose hash was
        # never stored would be rejected — so mint a fresh, stored one.
        if record.is_verified:
            self.stdout.write(
                self.style.WARNING(
                    f"{record.user} is already verified — issuing a fresh link."
                )
            )
        token = accounts_service.issue_link(record.user)
        record.refresh_from_db()
        url = f"{options['base_url'].rstrip('/')}{reverse('marketplace:verify_email', args=[token])}"

        self.stdout.write("")
        self.stdout.write(f"  User    : {record.user} <{record.user.email}>")
        self.stdout.write(f"  Status  : {'verified' if record.is_verified else 'pending'}")
        self.stdout.write(f"  Sent    : {record.sent_at:%Y-%m-%d %H:%M} (sends: {record.send_count})")
        self.stdout.write("")
        self.stdout.write(self.style.SUCCESS(f"  {url}"))
        self.stdout.write("")
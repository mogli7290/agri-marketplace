"""Review and verify the UPI IDs buyers are asked to trust.

    python manage.py verify_payout_methods                    # what needs checking
    python manage.py verify_payout_methods --all              # verified ones too
    python manage.py verify_payout_methods --verify 12        # mark one as checked
    python manage.py verify_payout_methods --unverify 12      # took it back

A buyer pays a farmer's VPA — the UPI ID like ``ravi@okaxis`` — only after
the page has warned them that the marketplace cannot refund them. The
"Verified by us" badge is the only thing distinguishing that address from one
anyone could have typed, so the decision gets recorded rather than being a
bare boolean nobody can date or attribute.

Nothing is verified automatically. This command shows what needs a human to
look at it and then writes down the answer.
"""

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from marketplace.models import Order, PayoutMethod
from marketplace.services import upi

CHECKLIST = """\
What a VPA is
  A VPA (Virtual Payment Address) is the UPI ID: the address money is sent
  to, in the form name@bank, e.g. ravi@okaxis or 8688141787-2@ybl. It is
  not an account number and not a phone number, though many are built from
  one. Every UPI app can receive money at any valid VPA.

How to check one is really the farmer's
  1. Format   It should look like name@bank. The code already enforces this,
              so a malformed one will not have been saved at all.
  2. Own app  Ask the farmer to open their own UPI app (GPay, PhonePe,
              Paytm) and read out the VPA shown there. It must match this
              one character for character. This is the step that matters.
  3. Name     The VPA's name part is usually the account holder's name. It
              matching the farmer's registered name is good corroboration.
  4. Small    For a real payout, send ₹1 from your own UPI app to the VPA
              and confirm it arrives. This proves the address resolves.
  5. Never    Never verify from a screenshot the farmer sent you. Anyone can
              send an image. Confirm in person or on a call where they open
              the app themselves.

  Verifying does not move money and cannot be undone by the farmer, so if
  you are unsure, leave it unverified and let the buyer choose the platform
  route instead, where they keep their refund protection.
"""


class Command(BaseCommand):
    help = "List farmers whose UPI ID has not been checked, and record the result."

    def add_arguments(self, parser):
        parser.add_argument(
            "--all", action="store_true",
            help="Include methods already marked verified",
        )
        parser.add_argument(
            "--verify", type=int, metavar="ID",
            help="Mark this payout method ID as checked, and record who checked it",
        )
        parser.add_argument(
            "--unverify", type=int, metavar="ID",
            help="Take verification back off this payout method ID",
        )
        parser.add_argument(
            "--by", metavar="USERNAME",
            help="Staff account to record as having checked it. Defaults to the "
                 "first superuser.",
        )
        parser.add_argument(
            "--checklist", action="store_true",
            help="Print how to check a UPI ID and exit",
        )

    def handle(self, *args, **options):
        if options["checklist"]:
            self.stdout.write(CHECKLIST)
            return
        if options["verify"] and options["unverify"]:
            raise CommandError("Choose either --verify or --unverify, not both.")

        if options["verify"]:
            return self._set(options["verify"], True, options["by"])
        if options["unverify"]:
            return self._set(options["unverify"], False, options["by"])

        self._list(include_verified=options["all"])

    # -- listing -------------------------------------------------------------

    def _list(self, include_verified: bool = False):
        methods = PayoutMethod.objects.filter(kind="upi").select_related(
            "user", "delivery_partner"
        ).order_by("is_verified", "-created_at")
        if not include_verified:
            methods = methods.filter(is_verified=False)

        rows = list(methods)
        if not rows:
            self.stdout.write(
                self.style.SUCCESS(
                    "No UPI IDs are waiting to be checked."
                    if not include_verified
                    else "No UPI IDs on file."
                )
            )
            return

        self.stdout.write("")
        self.stdout.write(f"{'ID':>5}  {'VPA':<28} {'farmer':<26} status")
        self.stdout.write("-" * 78)
        for method in rows:
            farmer = method.user
            name = self._name(method)
            status = self.style.SUCCESS("verified") if method.is_verified else self.style.WARNING("NOT checked")
            self.stdout.write(f"{method.pk:>5}  {method.upi_id[:28]:<28} {name[:26]:<26} {status}")

        pending = [m for m in rows if not m.is_verified]
        self.stdout.write("")
        if pending:
            self.stdout.write(
                self.style.WARNING(
                    f"{len(pending)} UPI ID(s) still unchecked. Buyers see "
                    f'"Not checked yet" next to these.'
                )
            )
            self.stdout.write("Mark one with:  python manage.py verify_payout_methods --verify <ID>")
        else:
            self.stdout.write(self.style.SUCCESS("Every UPI ID on file has been checked."))
        self.stdout.write("")
        self.stdout.write("How to check one:  python manage.py verify_payout_methods --checklist")

    @staticmethod
    def _name(method) -> str:
        if method.delivery_partner_id:
            return method.delivery_partner.name
        user = method.user
        if user is None:
            return "(no account)"
        profile = getattr(user, "farmer_profile", None)
        return profile.full_name if profile else user.username

    # -- recording -----------------------------------------------------------

    def _set(self, method_id: int, verified: bool, by_username: str = ""):
        try:
            method = PayoutMethod.objects.select_related(
                "user", "delivery_partner"
            ).get(pk=method_id)
        except PayoutMethod.DoesNotExist:
            raise CommandError(f"No payout method with ID {method_id}.")

        if method.kind != "upi":
            raise CommandError(
                f"#{method_id} is a {method.kind} method, not a UPI ID. "
                "Verify those in the admin."
            )
        if not upi.is_valid_vpa(method.upi_id):
            raise CommandError(f"#{method_id} does not contain a usable UPI ID.")

        if method.is_verified == verified:
            self.stdout.write(
                self.style.WARNING(
                    f"#{method_id} is already {'verified' if verified else 'unverified'}."
                )
            )
            return

        method.is_verified = verified
        who = None
        if verified:
            method.verified_at = timezone.now()
            who = self._acting_user(by_username)
            method.verified_by = who
        else:
            method.verified_at = None
            method.verified_by = None
        method.save(update_fields=["is_verified", "verified_at", "verified_by", "updated_at"])

        who_label = getattr(who, "username", None)
        self.stdout.write(
            self.style.SUCCESS(
                f"#{method_id} {method.upi_id} marked {'verified' if verified else 'unverified'}"
                + (f" by {who_label}." if verified and who_label else ".")
            )
        )
        if verified and who is None:
            self.stdout.write(
                self.style.WARNING(
                    "Recorded without a name against it: no superuser exists to "
                    "attribute this to. Create one, or re-run with --by <username>."
                )
            )

    @staticmethod
    def _acting_user(by_username: str = ""):
        """Whoever ran this, so the badge can name a person rather than a fact.

        A shell on a PaaS has no request user, so this falls back to the first
        superuser. Returns ``None`` when neither is available, which the caller
        warns about rather than hiding — an unattributed check on a payment
        address is a claim with nothing behind it.
        """
        User = get_user_model()
        if by_username:
            try:
                return User.objects.get(username=by_username)
            except User.DoesNotExist:
                raise CommandError(f"No user named {by_username!r}.")
        return User.objects.filter(is_superuser=True).order_by("pk").first()

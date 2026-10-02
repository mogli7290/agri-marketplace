"""Draft the weekly payout statements.

    python manage.py run_payouts --week-ending 2026-10-04
    python manage.py run_payouts --days 7 --dry-run
    python manage.py run_payouts --only partners

Drafts only. Nothing is paid and no money moves — a human reviews, approves and
then records the transfer reference. Run it weekly (cron, Render Cron Job).

Covers both sides of the money loop: farmers are paid for platform-collected
orders, delivery partners are paid for completed shipments.
"""

from datetime import timedelta
from decimal import Decimal, InvalidOperation

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from marketplace.models import DeliveryPartner, FarmerProfile, Listing
from marketplace.services import payouts as payout_service


class Command(BaseCommand):
    help = (
        "Create draft payout statements for farmers with settled orders and "
        "delivery partners with completed shipments."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--week-ending",
            help="Last day of the period (YYYY-MM-DD). Defaults to today.",
        )
        parser.add_argument(
            "--days",
            type=int,
            default=7,
            help="Length of the period in days, ending on --week-ending (default: 7)",
        )
        parser.add_argument(
            "--adjustment",
            default="0",
            help="Manual adjustment added to every statement, e.g. -50.00",
        )
        parser.add_argument("--notes", default="", help="Note stored on each statement")
        parser.add_argument(
            "--only",
            choices=["farmers", "partners"],
            help="Settle just one side of the loop (default: both)",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report what would be created without writing anything",
        )

    def handle(self, *args, **options):
        if options["week_ending"]:
            try:
                period_end = timezone.datetime.strptime(
                    options["week_ending"], "%Y-%m-%d"
                ).date()
            except ValueError as exc:
                raise CommandError("Use YYYY-MM-DD for --week-ending.") from exc
        else:
            period_end = timezone.localdate()

        days = options["days"]
        if days < 1:
            raise CommandError("--days must be at least 1.")
        period_start = period_end - timedelta(days=days - 1)

        try:
            adjustment = Decimal(options["adjustment"])
        except InvalidOperation as exc:
            raise CommandError("--adjustment must be a number, e.g. -50.00") from exc

        only = options["only"]
        if options["dry_run"]:
            self._report_dry(period_start, period_end, only)
            return

        created: list = []
        skipped: dict[str, str] = {}
        if only != "partners":
            farmers = payout_service.build_all_statements(
                period_start=period_start,
                period_end=period_end,
                adjustments=adjustment,
                notes=options["notes"],
            )
            created += farmers["created"]
            skipped.update(farmers["skipped"])
        if only != "farmers":
            partners = payout_service.build_all_partner_statements(
                period_start=period_start,
                period_end=period_end,
                adjustments=adjustment,
                notes=options["notes"],
            )
            created += partners["created"]
            skipped.update(partners["skipped"])

        self.stdout.write("")
        self.stdout.write(
            f"Period {period_start:%Y-%m-%d} → {period_end:%Y-%m-%d}"
        )
        self.stdout.write("")

        if not created:
            self.stdout.write(self.style.WARNING("Nothing was waiting to be settled."))
            return

        for payout in created:
            label = "partner" if payout.is_partner_payout else "farmer"
            unit = "shipments" if payout.is_partner_payout else "orders"
            self.stdout.write(
                f"  #{payout.pk} {payout.recipient_name[:40]:<42} "
                f"₹{payout.net_amount:>12}  {payout.order_count} {unit} ({label})"
            )

        total = sum((p.net_amount for p in created), Decimal("0.00"))
        self.stdout.write("")
        self.stdout.write(
            self.style.SUCCESS(
                f"{len(created)} statement(s) drafted · ₹{total} total"
            )
        )
        if skipped:
            self.stdout.write(f"{len(skipped)} recipient(s) had nothing to settle.")

    def _report_dry(self, period_start, period_end, only=None):
        self.stdout.write(f"Period {period_start:%Y-%m-%d} → {period_end:%Y-%m-%d}")
        self.stdout.write("")
        total = Decimal("0.00")
        count = 0

        if only != "partners":
            for farmer_id in FarmerProfile.objects.filter(
                pk__in=Listing.objects.values("farmer_id")
            ).values_list("pk", flat=True):
                summary = payout_service.pending_summary(farmer_id)
                if not summary["order_count"]:
                    continue
                count += 1
                total += summary["gross_amount"]
                self.stdout.write(
                    f"  farmer #{farmer_id}: {summary['order_count']} orders · "
                    f"₹{summary['gross_amount']} gross · "
                    f"₹{summary['commission_amount']} commission"
                )

        if only != "farmers":
            for partner in DeliveryPartner.objects.order_by("pk"):
                summary = payout_service.pending_partner_summary(partner)
                if not summary["shipment_count"]:
                    continue
                count += 1
                total += summary["gross_amount"]
                self.stdout.write(
                    f"  partner {partner.name[:30]}: {summary['shipment_count']} shipments · "
                    f"₹{summary['gross_amount']} delivery charges"
                )

        self.stdout.write("")
        self.stdout.write(
            self.style.WARNING(
                f"Would draft {count} statement(s) totalling ₹{total} (no writes made)."
            )
        )
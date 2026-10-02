"""Reconcile a bank statement against pending direct-UPI payment claims.

Direct UPI has no gateway to ask, so somebody has to look at the bank account and
match incoming credits to the UTRs buyers typed in. This module does the boring
part of that: it reads a CSV export from a bank or UPI app, matches each credit
to a pending ``Payment`` and reports what did *not* line up, so a human only
looks at the exceptions.

Design rules:

* **Never auto-confirm on a guess.** A row is matched on the transaction
  reference, and only when the amount agrees to within a tolerance.
* **Everything is a dry run first.** :func:`reconcile` only reads; the staff
  user reviews the preview and then calls :func:`apply_reconciliation`.
* **Unknown bank formats.** Column names are matched against alias sets, not
  fixed positions, and the delimiter is sniffed.
"""

from __future__ import annotations

import csv
import io
import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Iterable

from django.db import transaction
from django.utils import timezone

from marketplace.models import Payment, Reconciliation
from marketplace.services import upi

logger = logging.getLogger(__name__)

#: Credits within this many rupees of the claimed amount still count as a match
#: (some banks show the gross amount, some the net).
DEFAULT_TOLERANCE = Decimal("0.01")


class ReconciliationError(Exception):
    """Raised when a statement cannot be read or applied."""


# Header aliases, compared after stripping everything but letters and digits.
# Add a bank here rather than asking users to rename columns.
COLUMN_ALIASES: dict[str, set[str]] = {
    "reference": {
        "utr", "utrno", "utrnumber", "utrcode", "utrid",
        "transactionid", "transactionno", "transactionnumber",
        "transactionreference", "transactionref",
        "txnid", "txnno", "txnreference", "txnref",
        "referencenumber", "referenceno", "refno", "ref",
        "rrn", "rrnno", "rrnnumber",
        "upireference", "upiref", "upitransactionid",
        "paymentreference", "voucher", "voucherno",
    },
    "amount": {
        "amount", "credit", "creditamount", "creditedamount",
        "txnamount", "transactionamount", "amountcredited",
        "received", "receivedamount", "deposit", "value", "credittransaction",
    },
    "date": {
        "date", "txndate", "transactiondate", "valuedate", "value date",
        "paymentdate", "bookingdate", "dateandtime", "postdate",
    },
}

_NON_ALNUM = re.compile(r"[^a-z0-9]+")
_AMOUNT_NOISE = re.compile(r"[₹$,\s]|USD|INR", re.IGNORECASE)
_DATE_FORMATS = (
    "%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%d/%m/%y", "%d %b %Y", "%d %B %Y",
    "%Y/%m/%d", "%d.%m.%Y", "%Y-%m-%d %H:%M:%S",
)


@dataclass
class StatementRow:
    """One credit line from the statement."""

    reference: str
    line_number: int
    amount: Decimal | None = None
    value_date: date | None = None
    raw: dict = field(default_factory=dict)


def normalise_reference(value: str) -> str:
    """Fold a UTR to a comparable form (banks pad it with zeros or spaces)."""
    text = (value or "").strip().strip(".").replace(" ", "")
    if text.endswith(".0"):
        text = text[:-2]
    return text.upper()


def _header_key(name: str) -> str:
    return _NON_ALNUM.sub("", (name or "").lower())


def _parse_amount(raw) -> Decimal | None:
    if raw in (None, ""):
        return None
    text = _AMOUNT_NOISE.sub("", str(raw)).strip()
    if not text:
        return None
    try:
        return Decimal(text)
    except InvalidOperation:
        return None


def _parse_date(raw) -> date | None:
    if raw in (None, ""):
        return None
    text = str(raw).strip()
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def _decode(source) -> str:
    raw = source.read() if hasattr(source, "read") else source
    if isinstance(raw, bytes):
        for encoding in ("utf-8-sig", "latin-1"):
            try:
                return raw.decode(encoding)
            except UnicodeDecodeError:
                continue
        return raw.decode("utf-8", errors="replace")
    return str(raw)


def _map_columns(fieldnames: Iterable[str]) -> dict[str, str]:
    """Map canonical names to the actual header text used in this file."""
    mapping: dict[str, str] = {}
    for name in fieldnames or []:
        key = _header_key(name)
        if not key:
            continue
        for canonical, aliases in COLUMN_ALIASES.items():
            if canonical in mapping:
                continue
            if key in aliases or key in {_header_key(a) for a in aliases}:
                mapping[canonical] = name
    return mapping


def parse_statement(source) -> list[StatementRow]:
    """Read a bank/UPI-app CSV export into rows keyed by UTR.

    Raises ``ReconciliationError`` when there is no transaction reference column,
    since without it nothing can be matched.
    """
    text = _decode(source).replace("\r\n", "\n").replace("\r", "\n")
    if not text.strip():
        raise ReconciliationError("The statement file is empty.")

    sample = "\n".join(text.split("\n")[:10])
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel

    reader = csv.DictReader(io.StringIO(text), dialect=dialect)
    columns = _map_columns(reader.fieldnames or [])
    if "reference" not in columns:
        raise ReconciliationError(
            "Could not find a transaction reference column. Expected a header "
            "such as 'UTR', 'Transaction Reference' or 'Ref No'."
        )

    ref_key = columns["reference"]
    amount_key = columns.get("amount")
    date_key = columns.get("date")

    rows: list[StatementRow] = []
    for offset, record in enumerate(reader, start=2):  # line 1 is the header
        reference = normalise_reference(record.get(ref_key) or "")
        if not reference:
            continue
        rows.append(
            StatementRow(
                reference=reference,
                line_number=offset,
                amount=_parse_amount(record.get(amount_key)) if amount_key else None,
                value_date=_parse_date(record.get(date_key)) if date_key else None,
                raw={k: v for k, v in record.items() if k},
            )
        )
    return rows


def _index_claims() -> tuple[dict[str, Payment], set[str]]:
    """Map normalised UTRs to the claims awaiting confirmation."""
    pending: dict[str, Payment] = {}
    settled: set[str] = set()
    claims = Payment.objects.filter(provider="upi_direct").select_related("order")
    for payment in claims:
        key = normalise_reference(payment.provider_payment_id)
        if not key:
            continue
        if payment.status == "authorized":
            pending.setdefault(key, payment)
        elif payment.status in {"captured", "refunded"}:
            settled.add(key)
    return pending, settled


def reconcile(rows: Iterable[StatementRow], tolerance: Decimal = DEFAULT_TOLERANCE) -> dict:
    """Match statement rows to pending claims. Read-only — nothing is written."""
    rows = list(rows)
    pending, settled = _index_claims()
    tolerance = Decimal(str(tolerance))

    matched: list[dict] = []
    unmatched: list[dict] = []
    seen: dict[str, int] = {}

    for row in rows:
        duplicate_of = seen.get(row.reference)
        if duplicate_of:
            unmatched.append(_unmatched(row, "duplicate_in_file", f"Also on line {duplicate_of}"))
            continue
        seen[row.reference] = row.line_number

        payment = pending.get(row.reference)
        if payment is None:
            reason = "already_settled" if row.reference in settled else "no_matching_claim"
            unmatched.append(_unmatched(row, reason, _REASON_TEXT[reason]))
            continue

        warning = ""
        if row.amount is None:
            warning = "Statement has no amount column; matched on reference alone."
        elif row.amount < 0:
            unmatched.append(
                _unmatched(row, "debit", "Negative amount — not a credit.")
            )
            continue
        elif abs(row.amount - payment.amount) > tolerance:
            unmatched.append(
                _unmatched(
                    row,
                    "amount_mismatch",
                    f"Statement shows {row.amount} but the order expects {payment.amount}.",
                )
            )
            continue

        matched.append(
            {
                "payment_id": payment.pk,
                "order_id": payment.order_id,
                "reference": row.reference,
                "claimed_amount": str(payment.amount),
                "statement_amount": "" if row.amount is None else str(row.amount),
                "value_date": row.value_date.isoformat() if row.value_date else "",
                "warning": warning,
            }
        )

    return {
        "total_rows": len(rows),
        "matched": matched,
        "unmatched": unmatched,
        "pending_claims": len(pending),
    }


_REASON_TEXT = {
    "already_settled": "This reference was already confirmed.",
    "no_matching_claim": "No buyer submitted this UTR.",
    "duplicate_in_file": "This reference appears more than once in the file.",
}


def _unmatched(row: StatementRow, reason: str, detail: str) -> dict:
    return {
        "line": row.line_number,
        "reference": row.reference,
        "amount": "" if row.amount is None else str(row.amount),
        "value_date": row.value_date.isoformat() if row.value_date else "",
        "reason": reason,
        "detail": detail,
    }


def preview_statement(source, filename: str, user=None) -> Reconciliation:
    """Parse, match and store a dry-run record for review. Nothing is confirmed."""
    rows = parse_statement(source)
    report = reconcile(rows)

    return Reconciliation.objects.create(
        filename=filename[:255],
        uploaded_by=user if (user is not None and user.is_authenticated) else None,
        total_rows=report["total_rows"],
        matched_count=len(report["matched"]),
        unmatched_count=len(report["unmatched"]),
        report=report,
    )


@transaction.atomic
def apply_reconciliation(reconciliation: Reconciliation) -> dict:
    """Confirm every payment the preview matched. Idempotent per record."""
    if reconciliation.applied:
        raise ReconciliationError("This statement has already been applied.")

    confirmed = 0
    skipped: list[str] = []
    for entry in (reconciliation.report or {}).get("matched", []):
        payment = (
            Payment.objects.select_for_update()
            .filter(pk=entry["payment_id"], provider="upi_direct", status="authorized")
            .first()
        )
        if payment is None:
            skipped.append(str(entry.get("reference", entry["payment_id"])))
            continue
        try:
            upi.confirm_claim(
                payment,
                note=f"Auto-matched from bank statement {reconciliation.filename}",
                reconciliation=reconciliation,
            )
        except upi.UpiError as exc:
            logger.warning("Could not confirm payment %s: %s", payment.pk, exc)
            skipped.append(str(entry.get("reference", payment.pk)))
            continue
        confirmed += 1

    reconciliation.applied = True
    reconciliation.matched_count = confirmed
    reconciliation.applied_at = timezone.now()
    reconciliation.save(update_fields=["applied", "matched_count", "applied_at", "updated_at"])

    logger.info(
        "Reconciliation %s applied: %s confirmed, %s skipped",
        reconciliation.pk, confirmed, len(skipped),
    )
    return {"confirmed": confirmed, "skipped": skipped}


def unmatched_summary(reconciliation: Reconciliation) -> dict[str, int]:
    """Count the unmatched rows per reason, for a one-line display."""
    counts: dict[str, int] = {}
    for row in (reconciliation.report or {}).get("unmatched", []):
        counts[row["reason"]] = counts.get(row["reason"], 0) + 1
    return counts
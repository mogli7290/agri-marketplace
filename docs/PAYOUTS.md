# Payouts — paying the farmers and the transporters

The money loop, end to end:

```
Buyer pays (platform UPI)  →  money arrives  →  order marked paid
        ↓
   holds for PAYOUT_HOLD_DAYS
        ↓
weekly run drafts a statement per farmer  ─┐
                                           ├─ staff approves → transfer → record reference
Buyer's delivery charge  →  partner ships  ─┘   (completed shipments)
```

Two kinds of statement come out of one ledger:

| Recipient | What pays them | Source rows |
| --- | --- | --- |
| `FarmerProfile` | Produce value of platform-collected orders | `PayoutItem.order` |
| `DeliveryPartner` | Delivery charges the buyer paid | `PayoutItem.shipment` |

**Direct orders never appear here.** When a buyer pays a farmer's own UPI ID,
the money is already in the farmer's account and the platform never handled it.
Settling those would pay the same money twice. That is the running cost of
charging 0% on direct deals.

---

## 1. What is owed, and what is not

| Field on the statement | Meaning |
| --- | --- |
| `gross_amount` | What the recipient receives |
| `commission_amount` | Platform fee withheld at capture. Recorded, not netted off again |
| `delivery_amount` | Delivery charges. On a **partner** statement this equals `gross_amount` — all of it is transport money |
| `adjustments` | Manual corrections; negative claws money back |
| `net_amount` | `gross + adjustments` |

The commission is recorded for reporting, but nothing subtracts it at settlement
time: `order.total_amount` already included it, so `farmer_payout` is already
net. Deducting again would be a real bug — `test_totals_split_paid_and_outstanding`
pins that behaviour.

On a farmer statement `delivery_amount` is **informational** — the buyer paid it,
it belonged to the transporter, and the farmer is not paid it. That is exactly
why partner statements exist.

---

## 2. Eligibility

A **farmer order** is settleable when *all* of these hold:

- `payment_route == "platform"` (direct orders excluded)
- `payment_status == "paid"`
- `status` is `paid` or `delivered`
- older than the hold window (`PAYOUT_HOLD_DAYS`, default 7)
- not already claimed by an existing `PayoutItem`

A **shipment** is settleable when:

- `partner` is set
- `status == "completed"` — a `planned` or `dispatched` run has not earned the
  fee yet, and a `cancelled` one never will
- older than the same hold window
- not already claimed by an existing `PayoutItem`

The hold exists because a return, a quality claim or a refund can still arrive
after the money is collected. Waiting a week before paying out is the cheapest
insurance there is.

---

## 3. Why nothing can be paid twice

`PayoutItem.order` and `PayoutItem.shipment` are both **`OneToOneField`s**. That
is the whole guarantee — it lives in the database, not in application code, so two
settlement runs racing each other cannot produce two claims. A race surfaces as a
clean `PayoutError` ("Another settlement claimed one of these … first") rather
than a doubled payment.

Two check constraints back that up:

| Constraint | Guards against |
| --- | --- |
| `payout_has_exactly_one_recipient` | A statement paying a farmer, a partner, both, or neither |
| `payoutitem_has_exactly_one_source` | A line pointing at an order *and* a shipment |

`payouts.cancel()` deletes the items, which releases the orders or shipments back
into the queue for the next run.

---

## 4. State machine

```
draft ──approve──▶ approved ──mark_paid(reference)──▶ paid
  │                    │
  └──── cancel ────────┴──▶ cancelled   (items released)
```

- Only `draft` can be approved; only `approved` can be paid.
- A negative statement cannot be approved — that case needs a refund, not a payout.
- A `paid` statement cannot be cancelled.
- `reference` is required before a payout can be marked paid: it is the bank or
  gateway id that turns a number in a table into something auditable.

**This module never moves money.** It produces statements and records that a
human transferred funds. Wiring a real payout API (RazorpayX, Cashfree Payouts)
means replacing `mark_paid()` — nothing else changes.

---

## 5. The weekly run

```bash
python manage.py run_payouts --dry-run              # preview, writes nothing
python manage.py run_payouts --days 7               # this week, both sides
python manage.py run_payouts --week-ending 2026-10-04
python manage.py run_payouts --adjustment -50       # claw back ₹50 from each
python manage.py run_payouts --only partners        # farmers or partners only
```

Output looks like:

```
  #1 Ravindra (Shirur, Pune)      ₹    12000.00  14 orders (farmer)
  #2 Ravi Transport                ₹      1840.00  9 shipments (partner)

2 statement(s) drafted · ₹13840.00 total
3 recipient(s) had nothing to settle.
```

Drafts only. As a **Render Cron Job**:

```
python manage.py run_payouts --days 7 --notes "weekly settlement"
```

---

## 6. Staff workflow

- **Admin → Payouts** → select drafts → *Approve selected payouts*
- Transfer the money (NEFT/UPI — the recipient's verified `PayoutMethod` shows where)
- *Record selected payouts as paid* → enter the reference. One reference is shared
  by default; fill a per-payout row in the form to override it
- **Web:** `POST /staff/payouts/<id>/approve`, `/pay`, `/cancel`

Both sides watch their own statement list:

- Farmers: **`/earnings/`** — paid total, outstanding, orders waiting
- Transporters: **`/partner/earnings/`** — same shape, per completed shipment

They manage destinations separately (`/payout-methods/` and
`/partner/payout-methods/`) because a `DeliveryPartner` is not a `User` — its
destination hangs off the partner row (`PayoutMethod.delivery_partner`), guarded
by `payoutmethod_has_exactly_one_owner`.

Shipments gain lifecycle actions in admin (*Dispatch*, *Mark completed*, *Cancel*)
because their status now decides whether money is owed.

---

## 7. Known limits

| Gap | Note |
| --- | --- |
| No gateway payouts | Money leaves by manual NEFT/IMPS. `mark_paid` is the only place to change |
| No partial payouts | A statement is settled in full or cancelled |
| No payout emails | Both sides must check their earnings page; the mailer exists for this |
| No per-shipment commission | A partner is paid `planned_cost` in full; no take rate is modelled |
| No TDS / tax withholding | Indian agri payouts attract TDS; not implemented |
| Distance not priced per vehicle | `planned_cost` comes from the flat fee formula, not the vehicle class |

## 8. Tests

```bash
python manage.py test marketplace.tests.test_payouts
```

Covers eligibility (including that direct orders are never settled and that
unfinished shipments are never paid), the database-level no-double-payment
guarantee for both sources, every state transition, that cancel releases items,
partner payout destinations, and the settlement command across both sides.
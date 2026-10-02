# The demand board, messaging, and how the platform earns

Two halves of a marketplace:

- **Supply first** — a farmer lists produce and waits (`Listing`).
- **Demand first** — a buyer posts what they need and farmers come to them
  (`DemandRequest`). This is what wins in agriculture: a farmer with 20 kg of
  tomatoes cannot realistically be the "supply" half of a match.

---

## 1. The flow

```
Buyer                    Board                    Farmer
  │                        │                        │
  ├─ post request ────────▶│  "100 kg tomato,       │
  │   "100 kg tomato,      │   Pune, by Friday"     │
  │    Pune, budget ₹35"   │                        │
  │                        │◀──── offer ₹38, via ────┤
  │                        │      platform          │
  │◀─ offer ───────────────┤                        │
  │                        │   (message thread      │
  │                        │    opens here)         │
  ├─ accept ──────────────▶│                        │
  │   → Order created,     │                        │
  │     contact unlocked ──┼───────────────────────▶│
```

`RequestOffer` accepts only if the farmer has an **active listing for that crop**
— you cannot sell produce you have not published, and stock is reserved the same
way a normal order reserves it.

---

## 2. Contact details are gated — asymmetrically

Phone numbers and WhatsApp links sit behind the thread, and the gate opens
differently for each side. `deals_service.contact_details()` returns `None` while
locked, so a template cannot leak a number by accident.

| Who | Unlocks when |
| --- | --- |
| **Buyer** | A farmer makes an offer on their request |
| **Farmer** | An order exists (an offer was accepted) |
| **Staff** | Always |

The reasoning is that the two sides consented to different things.

A buyer who posts a request has **invited** farmers to answer it. Once one
actually responds they have engaged deliberately, so the buyer can call them —
chasing a supplier they cannot reach is the single most common way a deal dies.

A farmer has not. If they could read a buyer's number the moment they sent an
offer, posting a one-rupee offer would harvest every buyer's contact details off
a public board. That is precisely what the board must not become, so the farmer
waits until there is a real order behind the thread.

An **order** opens both sides completely: at that point a farmer who cannot call
their buyer to arrange a pickup has a real operational problem, and there is
nothing left to protect.

Sellers also only ever see **their own** offer on a request — never a rival's
price — so the board can't turn into an auction the platform doesn't control.

Sellers also only ever see **their own** offer on a request — never a rival's
price — so the board can't turn into an auction the platform doesn't control.

---

## 3. Two payment routes, and the honest fee answer

The seller chooses the route when making an offer. The buyer never chooses.

| | Through the platform | Direct to the farmer |
| --- | --- | --- |
| Buyer pays | the platform's UPI ID | the **farmer's** UPI ID |
| Who confirms | staff (CSV reconciliation) | the **farmer** confirms receipt |
| Platform fee | `PLATFORM_FEE_PERCENT` (default 2%) | **0%** |
| Farmer receives | subtotal, settled later | 100% immediately |
| Platform revenue | the fee | **nothing** |

That last row is the whole point, and it is worth being blunt about: **you chose
to charge a fee only on platform-collected orders, so direct orders earn you
nothing.** That is a deliberate trade — sellers love it (instant money, no
withholding), buyers love it (no fee), and it makes the product viable in a
market where 2% is a real barrier to entry.

The consequences are structural, not cosmetic:

- `orders.calculate_platform_fee(subtotal, "direct")` returns `0.00`, so a direct
  order's `total_amount` equals its `subtotal` and `farmer_payout` is the whole
  thing. There is no code path that accidentally charges a fee on a direct order.
- The platform cannot see the money move, so it cannot guarantee it. On direct
  orders the *farmer* is the one who sees the credit in their UPI app, so they
  are the one who confirms — `upi.confirm_seller_receipt()`. The platform takes
  no part in that transaction.
- Direct transfers are therefore **not** reconcilable by the platform. Staff
  cannot produce a settlement report for that money, because it was never theirs.

### If you later want revenue from direct deals

The standard options, in the order most agri marketplaces adopt them:

1. **Commission on the seller, settled later** — record `commission_due` on each
   order and deduct it from the next payout. Needs a payout ledger (next on the
   roadmap) to work.
2. **Monthly invoice to the seller** — charge for deals closed that month.
3. **Subscription / listing fee** — sell the response inbox, not the transaction.

---

## 4. Payout details

`PayoutMethod` stores where a seller wants money sent: a UPI ID or a bank account.

- It is **payout-destination data only**. Nothing in the codebase uses it to take
  money *from* a user.
- Bank account numbers are masked everywhere they render
  (`masked_account_number` → `XXXX9012`).
- A farmer must have a **UPI ID on file** before the direct route is offered at
  all — otherwise the option would be presented and then broken.
- Staff mark methods verified in the admin once the account name matches. An
  unverified method still works, so a farmer is never blocked, but it is flagged.

---

## 5. Messaging

One `Conversation` per (request, farmer) pair, created automatically with the
first offer. Both sides post; `deals_service.post_message()` enforces
participation. There is no read receipt and no push notification yet — see the
roadmap.

---

## 6. Where things live

| Piece | File |
| --- | --- |
| Rules (requests, offers, contact unlock, payout details) | `marketplace/services/deals.py` |
| Direct payment claim + seller confirmation | `upi.record_seller_payment`, `upi.confirm_seller_receipt` |
| Route-aware fees | `orders.calculate_platform_fee(subtotal, route)` |
| Pages | `request_form`, `requests`, `my_requests`, `request_detail`, `conversation`, `conversations`, `payout_methods` |

## 7. Tests

```bash
python manage.py test marketplace.tests.test_deals
```

Covers the fee rule (direct is always zero), the asymmetric contact gate,
rivals' offers being invisible, only the buyer accepting, only the farmer
withdrawing or confirming a direct payment, and account-number masking.
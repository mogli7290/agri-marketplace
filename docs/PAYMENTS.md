# Payments — UPI, two ways

There are **two independent payment routes**, both UPI-first, and they can be
enabled separately (or not at all):

| Route | Needs | Good for |
| --- | --- | --- |
| **Razorpay Checkout** — `marketplace/services/payments.py` | API keys (free test keys) | UPI + cards + netbanking, automatic reconciliation |
| **Direct UPI** — `marketplace/services/upi.py` | one UPI ID (VPA) | no gateway, no KYC, no fees; settled manually |

Both write to the same `Payment` model, so the order timeline, admin and API
look identical whichever route was used. With neither configured the app is
unaffected: pay buttons are hidden and orders stay `pending`.

---

## 1. Why Razorpay

**Razorpay** — India-first, with **native UPI** plus cards, netbanking and
wallets in one checkout.

- UPI is a first-class payment method, which is what farmers and buyers here use.
- Free to start: test keys work with **no KYC and no business account**.
- Simple REST API with a well-documented HMAC signature scheme.

Alternatives that would also work: Cashfree, PhonePe Payment Gateway, PayU.
`payments.py` is the only file that speaks to Razorpay, so swapping gateways
means rewriting one module.

---

## 2. What is implemented

| Piece | File |
| --- | --- |
| Gateway client (orders, fetch, refunds, signatures) | `marketplace/services/payments.py` |
| Settlement + webhook state machine | `payments.record_capture`, `payments.handle_webhook_event` |
| Direct-UPI links, claims, confirmations, reversals | `marketplace/services/upi.py` |
| Payment records | `Payment` model in `marketplace/models.py` |
| Web: start / verify / webhook / UPI claim | `PaymentInitiateView`, `PaymentVerifyView`, `PaymentWebhookView`, `UpiPaidView` |
| API: `initiate_payment`, `verify_payment`, `upi_paid` | `marketplace/api/views.py` |
| Staff settlement + refunds | `PaymentAdmin.confirm_upi`, `.refund_razorpay`, `.reverse_upi` |
| Checkout UI | inline Razorpay Checkout JS + UPI QR in `templates/dashboard/order_detail.html` |
| Config | `RAZORPAY_*` / `UPI_ID` settings, documented in `.env.example` |

---

## 3. The gateway flow (and the security checks)

```
Buyer clicks "Pay ₹X"
        │
        ▼
[1] POST /orders/<id>/pay/         amount computed server-side from the ORDER
        │                          (total_amount × 100 → paise)
        ▼
[2] Razorpay order created         Basic Auth (key id + secret)
        │                          + Idempotency-Key: agm-order-<id>-<amount>
        │                          a Payment row is written (status="created")
        │                          an in-flight payment is REUSED on a re-click
        ▼
[3] Razorpay Checkout opens       UPI / card / netbanking
        ▼
[4] POST /payments/verify/        razorpay_order_id | payment_id | signature
        │                          ① HMAC-SHA256("order_id|payment_id", secret)
        │                          ② that provider_order_id must belong to a
        │                            Payment row belonging to THIS order   ← CHECK 2
        ▼
[5] Payment → "captured";          order_service.mark_order_paid(order)
    order → "paid"
        ▲
        │
[6] /payments/webhook/            X-Razorpay-Signature over the raw body
                                   HMAC-SHA256(body, webhook_secret)      ← CHECK 1
                                   handles payment.captured, payment.authorized,
                                   order.paid, payment.failed, refund.processed
```

**Why two verifications:** the browser callback (`[4]`) is the fast path for
instant UX, but a browser can be tampered with. The webhook (`[6]`) is the
authoritative server-to-server confirmation. Both call the same service
functions, and both are idempotent, so a duplicate delivery settles nothing twice.

**Why check ② matters.** The signature only proves *"this money was paid to
order A"*. Without binding it to the order being settled, a buyer could pay ₹10
for their own order and replay that callback against someone else's ₹5,000
order. `payments.record_capture()` matches on `(order, provider_order_id)` and
refuses anything else.

---

## 4. Direct UPI (no gateway)

```
Order page  →  upi://pay?pa=agrimarket@okaxis&am=<exact total>&tr=order_7
             →  QR code of the same string, or "Open UPI app"
Buyer pays in GPay/PhonePe/Paytm, then pastes the UTR
        ▼
POST /orders/<id>/upi-paid/       upi.record_claim(order, utr)
                                 → validates the VPA and the UTR shape
                                 → refuses a second claim on the same order
                                 → refuses a UTR already used on another order
                                 → Payment(status="authorized")   ← NOT settled
        ▼
Reconcile  →  staff upload the bank/UPI-app CSV
          →  credits are matched to those UTRs
          →  preview first, confirm second      → Payment "captured", order "paid"
```

Claiming never settles an order on its own, so a fake "I've paid" cannot mark
an order paid.

### 4.1 Reconciliation — the part that makes it hands-off

Without a gateway there is no callback, so the only source of truth is your bank
account. **Reconcile** (`/payments/reconcile/`, staff only) closes that gap: drop
in the CSV you export from your bank or UPI app and it matches incoming credits
to the references buyers submitted.

```
Upload CSV ──► parse ──► match ──► PREVIEW (nothing settled)
                                    │
                       staff review │ press "Confirm"
                                    ▼
                          matched claims → captured, orders → paid
```

**What it will not do:** settle a row whose amount disagrees with the order, a
UTR nobody submitted, a reference that appears twice in the file, a debit, or a
reference that was already confirmed. Each lands in a *Needs a human* table with
the reason.

**Reading messy bank exports.** Column order is irrelevant, and headers are
matched against alias sets:

| Meaning | Accepted headers (any of) |
| --- | --- |
| Reference | UTR, UTR No, RRN, Transaction Id, Txn Reference, Ref No, Voucher |
| Amount | Amount, Credit, Received, Value, Deposit *(optional — matches on reference alone if absent)* |
| Date | Date, Txn Date, Value Date, Booking Date *(optional)* |

Commas, semicolons and tabs are detected, Excel's UTF-8 BOM is stripped, `₹`
and thousands separators are ignored, and references are normalised (`" 412233445566 "`
and `"412233445566.0"` both match `412233445566`).

Amounts are compared with a one-paise tolerance, because some banks show the
gross credit and some the net.

Every run is stored as a `Reconciliation` row (filename, uploader, counts, full
report) and linked from the matched `Payment` rows, so "who confirmed this and
against what statement?" always has an answer.

### 4.2 Without CSV at all

The admin still works payment-by-payment: **Payments → "Confirm UPI payment"**
for a single claim, "Record a manual UPI reversal" to give money back. Use that
for one-offs and the reconciler for the daily batch.

---

## 5. How to switch each one on

### Razorpay

1. Sign up at <https://dashboard.razorpay.com> → **Settings → API Keys → Generate Test Keys**.
2. Set the environment:

```env
RAZORPAY_KEY_ID=rzp_test_xxxxxxxx
RAZORPAY_KEY_SECRET=xxxxxxxxxxxxxxxx
RAZORPAY_WEBHOOK_SECRET=choose-a-long-random-string
RAZORPAY_CURRENCY=INR
```

3. Register the webhook: **Settings → Webhooks → Add**

- **URL:** `https://<your-domain>/payments/webhook/`
- **Secret:** the same value as `RAZORPAY_WEBHOOK_SECRET`
- **Events:** `payment.captured`, `payment.authorized`, `payment.failed`, `refund.processed`

4. Test with Razorpay's test handles:

| Method | Test value | Result |
| --- | --- | --- |
| UPI | `success@razorpay` | Payment succeeds |
| UPI | `failure@razorpay` | Payment fails |

### Direct UPI

```env
UPI_ID=yourname@okaxis      # any VPA you can receive on
UPI_PAYEE_NAME=AgriMarket   # shown inside the buyer's UPI app
```

Restart; the UPI block (QR + "Open UPI app" + UTR box) appears on unpaid orders.

---

## 6. Going live (the honest part)

Razorpay **test mode works today, free, with no KYC**. Live mode requires a
merchant account with completed KYC — registered business details, PAN, and a
bank account in the business's name. An individual generally cannot accept live
payments through Razorpay.

Direct UPI needs no KYC at all, but it is not automated: someone must check the
credit and confirm it, and the platform account has to hold the money until
farmers are paid out.

---

## 7. What is NOT implemented yet (read this)

| Not done | What it means | How to add it |
| --- | --- | --- |
| **Farmer payouts / split settlement** | Money lands in the platform account; `Order.farmer_payout` records what the farmer is owed, but no transfer happens | Integrate **Razorpay Route** (linked accounts + transfers) or **RazorpayX Payouts** |
| **Invoices / receipts** | No PDF | Render from the order + `Payment` rows |
| **Gateway auto-reconciliation** | Nothing polls the gateway for missed webhooks (direct UPI *is* reconciled, by CSV) | Periodic `payments.fetch_payment()` sweep comparing against `Payment` rows with no provider id |
| **Partial refunds in the admin UI** | `create_refund(amount=…)` supports it, the admin action always refunds in full | Add a refund form on the order page |
| **Scheduled CSV import** | Reconciliation is manual: staff upload and press confirm | Run `reconcile` from a cron job with a watched folder |
| **Partial-order statements** | Every credit must be a full order payment | Add a deposit/advance model |
| **Email/SMS receipts** | No notifications | Hook `mark_order_paid` / `mark_order_refunded` into a message backend |
| **Payout to FPOs** | Same as farmers | Same as Razorpay Route |

Everything else — initiation, verification, webhook handling, refunds, direct
UPI claims/confirmations/reversals, and the order state machine — is in place.

---

## 8. Testing

The payment logic has automated tests that need no network and no keys:

```bash
python manage.py test marketplace.tests.test_payments_security marketplace.tests.test_upi marketplace.tests.test_reconciliation
```

They cover the signature maths, the order-binding check (a valid payment for
order A cannot settle order B), webhook idempotency and replay safety, refusal
to pay cancelled orders, direct-UPI claim guards, the refund path, and every
statement-matching rule (wrong amount, duplicate row, debit, unknown UTR).

Quick manual check from the shell:

```bash
python manage.py shell -c "
from marketplace.services import payments, upi
print('razorpay configured:', payments.is_configured())
print('direct upi configured:', upi.is_configured(), upi.is_valid_vpa('name@okaxis'))
print('tampered signature valid?', payments.verify_checkout_signature('order_1','pay_1','nonsense'))
"
```

---

## 9. Troubleshooting

| Symptom | Cause | Fix |
| --- | --- | --- |
| No "Pay" button | `RAZORPAY_KEY_ID`/`SECRET` unset | Set both and restart |
| No UPI block | `UPI_ID` unset, or cancelled/already-paid order | Set `UPI_ID`; check the order state |
| "Could not initiate payment" | Wrong/revoked keys, or network blocked | Check keys; check the server can reach `api.razorpay.com` |
| "Payment verification failed" | Signature mismatch — usually the wrong key secret | Re-copy `RAZORPAY_KEY_SECRET` |
| "We could not match this payment to the order" | Payment was created for a different order, or the order was already settled | Retry from the order page; if it persists, reconcile with the gateway |
| Order stays unpaid after paying | Webhook not registered/misconfigured | Verify the webhook URL, secret, and subscribed events |
| `400` on `/payments/webhook/` | `RAZORPAY_WEBHOOK_SECRET` doesn't match the dashboard | Make them identical |
| UTR rejected as "not a reference" | Fewer than 6 characters, or contains spaces | Copy the full 12-digit UTR from the UPI app |
| "That reference has already been recorded" | The UTR was used on another order | Genuine mistake — verify the credit manually and confirm/reverse in admin |
| "Could not find a transaction reference column" | The CSV has no UTR/Ref/Transaction column | Export the detailed (not summary) statement from your bank |
| A row shows as "No buyer submitted this UTR" | Cashback/interest/transfer-in line, or a buyer mistyped the reference | Ignore it, or confirm the matching order by hand in the admin |
| A row shows as "amount_mismatch" | Partial payment, or a bank fee | Settle it manually; the reconciler will not guess |
# Notifications — the emails that make it feel alive

A farmer on a phone is not going to open a dashboard to find out whether an order
was paid. These messages are the difference between a marketplace and a
spreadsheet.

```
order_placed ────────▶ the farmer: somebody wants your produce
payment_confirmed ───▶ the buyer:  your money landed
offer_received ──────▶ the buyer:  a farmer answered your request
offer_accepted ──────▶ the farmer: your offer was taken up
order_dispatched ────▶ the buyer:  it's on the road
order_delivered ─────▶ both sides: it arrived
payout_settled ──────▶ the recipient: your money is on the way
```

---

## 1. Three rules

**Nothing raises.** A bounced email must not roll back the order it was about.
`messaging.send_email` swallows delivery failures; `notifications` additionally
catches a rendering failure per recipient, so a typo in a template is logged
rather than turned into a 500 for the person placing an order.

**Nothing is sent before the transaction commits.** Every event goes through
`transaction.on_commit`, so nobody is told an order was placed before the
transaction that placed it has actually succeeded — or at all, if it rolled back.
This is the whole reason `notify()` returns `0` when it defers: the mail has been
*scheduled*, not sent.

**No queue, no cost.** Sending happens on the request thread. With no Celery and
no Redis that is the trade: a slow mail provider adds latency to the request that
triggered it, but nothing is ever silently dropped. When volume justifies it, the
only change needed is to swap `_deliver` for a `.delay()`.

---

## 2. Where the hooks live

Notifications are fired from the **service layer**, not from views, so the web
UI, the DRF endpoints and the payment webhook all produce the same mail.

| Service | Event | Fires when |
| --- | --- | --- |
| `orders.create_order` | `order_placed` | Stock is reserved |
| `orders.mark_order_paid` | `payment_confirmed` | Only on a *real* transition |
| `orders.advance_status` | `order_dispatched` | status becomes `in_transit` |
| `orders.advance_status` | `order_delivered` | status becomes `delivered` |
| `deals.create_offer` | `offer_received` | A farmer answers a demand request |
| `deals.respond_offer` | `offer_accepted` | The buyer accepts |
| `payouts.mark_paid` | `payout_settled` | A farmer **or partner** statement is settled |

Two deliberate omissions:

- **Internal transitions are silent.** `confirmed` and `picked_up` produce no
  mail. A notification that fires on every step is a notification people stop
  reading.
- **A replayed webhook sends nothing.** `mark_order_paid` is idempotent, so a
  second webhook replay must not produce a second "thanks for your payment".

---

## 3. Anatomy of an event

An event is a subject line plus a pair of templates. Subjects live in
`notifications.SUBJECTS` rather than in the templates so the whole catalogue of
what the platform says can be read in one place:

```python
SUBJECTS = {
    "order_placed": "New order #{order_id} · {site_name}",
    ...
}
```

Each event renders `templates/email/<event>.txt` and, if present,
`<event>.html`. The HTML variants all extend `email/_layout.html`, so the
branding lives in exactly one place. Absolute links are built from `SITE_URL` —
a relative `/orders/1/` is dead in an email client.

### The host has to be right, including the port

A link with the wrong port is exactly as dead as a link with the wrong path, and
it is the failure you notice last — the mail is sent, the server logs nothing
wrong, and the only symptom is a "site can't be reached" on a phone.

```bash
# What does a link in a real email actually point at?
python manage.py send_test_email --to you@example.com --check-only
```

`--check-only` prints the configured `SITE_URL` and warns when it is unset,
has the wrong scheme, or is a loopback address.

**If you read mail on a phone, `SITE_URL` must be your machine's LAN address.**
`127.0.0.1` on a phone means the phone, which has no server on it:

```bash
# .env — dev server started with: python manage.py runserver 0.0.0.0:8001
DJANGO_ALLOWED_HOSTS=localhost,127.0.0.1,<your-lan-ip>,testserver
DJANGO_CSRF_TRUSTED_ORIGINS=http://localhost:8001,http://<your-lan-ip>:8001
SITE_URL=http://<your-lan-ip>:8001
```

Find the address with `hostname -I` or `ip -4 addr show scope global`, and
confirm the port your server actually bound to with `ss -ltn | grep 800`.

If `SITE_URL` is a loopback address but the request came from a real host,
`accounts_service.site_base_url()` trusts the request instead, so a forgotten
`SITE_URL` degrades to a working link rather than a dead one. A configured
non-loopback `SITE_URL` always wins, which is what production needs.

---

## 4. Trying it out

```bash
# Check the configuration without sending anything
python manage.py send_test_email --to you@example.com --check-only

# Send one real message, plus a sample of every notification
python manage.py send_test_email --to you@example.com
```

The previews use fabricated data and are addressed only to the address you pass,
so no real user is ever mailed.

To see the real thing end to end: place an order on the demo accounts and watch
the mail arrive.

---

## 5. Turning it off

```bash
NOTIFICATIONS_ENABLED=false
```

`notifications.notify()` checks this first, so every event is silenced without
touching a single call site. Useful while a mail provider is misbehaving, or for
a demo where you do not want the confirmation mail confusing anyone.

Verification mail is **not** affected — that one goes through
`accounts.issue_verification` directly, because a signup with no confirmation
link is a signup nobody can complete.

---

## 6. Known limits

| Gap | Note |
| --- | --- |
| Synchronous sending | A slow provider slows the request that triggered it |
| Free-tier deliverability | Brevo only delivers to your own address until a sending domain is verified |
| No in-app inbox | Everything lands in email; there is no notification feed in the UI |
| No preferences page | A user cannot opt out of, say, payout mail only |
| No retry queue | A transient provider error is logged and lost |
| No SMS or WhatsApp | Email only — see §7 |

---

## 7. Why there is no SMS

Investigated and deliberately declined. Recording it here so the research is not
repeated, and so the constraint is understood rather than rediscovered.

**Indian SMS is not reachable without a business PAN.** TRAI rules require DLT
registration for all business SMS, and registering a DLT entity needs a PAN.
There is also no permanent free tier from any Indian provider — only trial
credits.

**Two routes were evaluated:**

| Option | Blocked by |
| --- | --- |
| Twilio (domestic gateway) | DLT registration, and therefore PAN |
| Twilio (international gateway) | No PAN needed — but no free tier either. ~$0.03 (≈₹2.50) per message, no monthly fee |
| WhatsApp Business Cloud API (Meta) | Charged per message outside the 24-hour customer service window |
| Email (Brevo) | **None.** 300/day, already wired up |

Meta's own pricing page confirms the WhatsApp position: non-template messages are
free only *inside* an open customer service window, and a cold "your order was
paid" alert to somebody who has not messaged in a day is a **charged utility
template**. The "1,000 free messages/month" figures circulating in blog posts
refer to the older conversation-based pricing model and are not the current deal.

**Decision: email only.** Every route costs money per message, and this project
is built on a zero-budget rule.

### What that leaves broken

The buyer-side delivery code is the one real casualty. A buyer who does not check
email cannot read the code to their driver, so the stop cannot be closed without
staff.

Two mitigations are in place, both free:

- The driver can **issue the code themselves** from the shipment page
  (`DeliveryCodeView`), so a buyer who never clicked "send me my code" is not a
  dead end — the driver knocks, presses one button.
- Stops with no code issued are **flagged** in the driver select, so the problem
  is visible before the driver reaches the door rather than after.

That is a mitigation, not a fix. A genuine fix is a paid SMS provider, and the
work is small once one is chosen: `notifications.notify()` is already the single
choke point, and the delivery-code event is the first thing that would move onto
it. Adding a spend cap means one counter and one comparison in
:meth:`~marketplace.services.notifications._deliver`.

## 8. Tests

```bash
python manage.py test marketplace.tests.test_notifications
```

Covers: the right person receives each event and nobody else, nothing is sent
for a rolled-back transaction, a broken backend and a broken template both leave
the order intact, a replayed payment sends nothing, and the master switch works.
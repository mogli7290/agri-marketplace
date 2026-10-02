# Proof of delivery

Without it, "the driver says he dropped it, the buyer says nothing arrived" is
unresolvable. With it, there is a code only the recipient can read, a timestamp, a
location, and optionally a photo or a signature — and the delivery charge only
becomes payable once the buyer is on board.

```
buyer requests code ──▶ emailed to the buyer (hashed at rest)
                              │
driver arrives ─────────▶ types the code + captures photo/signature
                              │
                          stop closed ──▶ shipment completed
                              │
                          buyer confirms or disputes
                              │
                  dispute ──▶ delivery charge held, no payout
                              │
                        staff resolve ──▶ release / refund
```

---

## 1. Three kinds of evidence

Any one is enough on its own, because real drivers have patchy signal and no
camera. A driver with a code and nothing else is fully covered.

| Evidence | What it proves | Cost |
| --- | --- | --- |
| **Delivery code** | Only the person receiving the goods can read it out. Strongest available | Free — a six-digit number in a text column |
| **Signature** | Someone signed at the door | Free — an SVG path in a text column, not an image |
| **Photo** | The load, at the address | Object storage; needs `USE_S3=true` to survive a redeploy |

The code is the anchor; photo and signature are corroboration. The signature is
stored as vector path data specifically so it costs one text column and cannot
rot the way a stored bitmap eventually will.

`DeliveryProof.evidence_count` reports how many kinds a proof carries, and is
what the admin and the emails surface.

---

## 2. The code is hashed, and rate limited

Two properties, both pinned by tests:

**Only the hash is stored.** Exactly as with email verification: a leaked
database must not let anyone confirm a delivery they did not receive.

**Six wrong guesses and the stop is cold.** Six digits is a million
possibilities, which is nothing to brute force. `MAX_CODE_ATTEMPTS` (5) caps it,
and exhausting the budget forces a *fresh* code rather than locking the driver
out mid-round. The counter increments on every attempt, right or wrong, so it
cannot be reset by simply trying again.

Codes expire after 24 hours (`CODE_MAX_AGE_SECONDS`), and issuing a new one
supersedes the old — a screenshot goes stale.

### Issued is not verified

`has_otp` means `otp_confirmed_at is not None` — the buyer *read it back to the
driver*. `has_code_issued` means a number is sitting in the database waiting.
Only the first counts as evidence, because an issued code proves nothing at all.

---

## 3. The staff override, on purpose

`record_proof` refuses a proof with no code from a driver. It does not refuse one
from staff.

Phones fail, addresses get renamed, produce gets handed to a gatekeeper who never
saw the code. A system that models none of that gets bypassed in the field, and
bypassed means invisible. So the override exists, and it is recorded against the
actor who used it rather than being impossible.

The same reasoning applies to `close_shipment(force=True)`: a shipment whose stop
can never be closed honestly (buyer unreachable, address gone) can still be
closed, with a warning in the log naming who forced it.

---

## 4. How it gates the money

This is the part that matters commercially. A transporter cannot claim a
delivery charge for a drop nobody witnessed:

- `Shipment.status == "completed"` makes the delivery charge payable.
- `close_shipment` refuses while any delivery stop is unproven.
- A **disputed** proof excludes the whole shipment from
  `payouts.eligible_shipments`.

So the sequence is: proof → stop closed → shipment completed → charge payable.
Staff *resolving* a dispute as `release` clears the hold and the shipment becomes
payable; `refund` resolves it in the buyer's favour and leaves the refund to be
raised against the payment.

Paying first and chasing afterwards would leave the transporter chasing the buyer
for money the platform had already committed to.

---

## 5. The pages

| Who | Where | What |
| --- | --- | --- |
| Buyer | `/orders/<id>/` | "Send me my delivery code", the code after requesting, the evidence on file, and **Confirm** / **Dispute** |
| Transporter | `/shipments/<id>/` | Pick the stop, type the code, draw a signature, attach a photo, submit. Also **"Email the buyer a delivery code"** and a flag on stops where the buyer never requested one |
| Staff | `/orders/<id>/` (on a disputed proof) | Release the money, or refund the buyer |
| Staff | `/admin/marketplace/deliveryproof/` | Search, filter by partner, confirm or reopen |

The signature pad is ~40 lines of vanilla JS. It records an SVG path in a hidden
input — no canvas export, no upload, no image processing.

---

## 6. Notifications

| Event | To | Why |
| --- | --- | --- |
| `delivery_code` | **Buyer only** | The one notification carrying a secret. Never the driver |
| `delivery_recorded` | Buyer | A delivery was claimed; here's the evidence, confirm or dispute |
| `delivery_confirmed` | Transporter | Their charge is now undisputed and counts towards payout |
| `delivery_disputed` | Transporter | Told before they start chasing payment for a contested drop |

All four go out through `transaction.on_commit` like every other notification —
see `docs/NOTIFICATIONS.md`.

---

## 7. Known limits

| Gap | Note |
| --- | --- |
| Photos need `USE_S3=true` | On Render's ephemeral container FS, a photo dies with the redeploy. The code and signature still survive — they're in the database |
| No photo redaction | Faces, house numbers and vehicle plates are stored as-is. Needs a retention policy and a privacy review before real use |
| No buyer-side SMS fallback | The code reaches the buyer by email only, so a buyer who never checks it needs the driver to press "Email the buyer a delivery code" at the door. Real fix is a paid SMS provider — see `docs/NOTIFICATIONS.md` §7 |
| One proof per stop | A stop cannot be re-recorded once proven; staff reopen it from the admin |
| No partial delivery | A short or damaged delivery has to be recorded as a whole stop and disputed afterwards |
| No automatic refund | Resolving as `refund` flags the dispute; raising the actual refund is still a manual step |

## 8. Tests

```bash
python manage.py test marketplace.tests.test_proof_of_delivery
```

Covers: the code reaches the buyer and nobody else, only its hash is stored, a new
code supersedes an old one, brute force is capped and a correct code still works
after wrong ones, a driver cannot close a stop without the code but staff can,
staff overrides are attributed, a dispute holds the transporter's money until
resolved, and every web route's permissions.
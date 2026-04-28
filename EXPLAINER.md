# Playto Payout Engine — Design Decisions

> Five choices that make this system safe to move real money.

---

## 1. Balance is never stored — it's always computed

**The risk:** Saving balance as a column on the merchant record means any failed write or concurrent update can leave it stale or wrong.

Instead, every credit and debit is an immutable row in a `LedgerEntry` table. Balance is the live sum of those rows. Nothing is pre-computed.

**Why this matters:**
- **No corruption risk** — once a row is written, it's permanent. No partial updates.
- **Full audit trail** — every balance change has a timestamp and a reason.
- **Paise, not floats** — all amounts are stored as integers (₹100.01 = 10001 paise). Floating-point rounding errors are structurally impossible.

```python
def get_available_balance(self):
    total = self.ledger_entries.aggregate(total=Sum('amount_paise'))['total'] or 0
    held  = self.payouts.filter(status__in=['pending', 'processing']).aggregate(
                total=Sum('amount_paise'))['total'] or 0
    return total - held
```

---

## 2. Two concurrent requests cannot both drain the same balance

**The risk:** Two threads both read the balance as ₹1000. Both validate. Both create payouts. You've sent ₹1100 from a ₹1000 account.

Before any balance check, I acquire a `SELECT FOR UPDATE` lock on the merchant row. Any other thread trying to read that row blocks until the first transaction commits.

**Why this matters:**
- **Validation happens under the lock** — the balance can't change between the check and the write.
- **All pending payouts are also locked** — a second `process_payout` task can't race the first one.
- **Why not Python-level checks?** They're inherently racy. By the time Python evaluates your `if`, another thread may have already written.

```python
with transaction.atomic():
    merchant = Merchant.objects.select_for_update().get(id=merchant_id)
    # balance is now frozen for this transaction
    if amount_paise > available:
        raise ValueError("Insufficient balance")
    Payout.objects.create(...)
```

---

## 3. A retry is identical from the original request

**The risk:** The server processes a payout but the response is lost. The client retries. Without idempotency, two payouts get created.

The client sends a UUID in the `Idempotency-Key` header. The server stores both the key and the full response. If it sees the same key again within 24 hours, it returns the cached response — no new payout is created.

**Why this matters:**
- **Key lookup happens first** — before any side effects, so there's no window for a duplicate.
- **The response is always consistent** — whether the payout is still pending or already completed, the client gets the same original response back.
- **Scoped per merchant** — keys are unique per merchant, so different merchants can use the same UUID safely.

```python
if idempotency_key:
    cached = get_idempotency_response(merchant, idempotency_key)
    if cached:
        return Response(cached)  # exact same response as the first time
```

---

## 4. Payouts can only move forward, never backward

**The risk:** A bug, a retry, or a manual intervention tries to move a completed payout back to pending. Money gets re-sent.

Every status change goes through a `transition_to()` method that validates the move against an allowed-transitions map. Illegal moves raise an exception immediately.

**Why this matters:**
- **Terminal states are enforced** — `COMPLETED` and `FAILED` cannot transition to anything.
- **Race conditions surface as errors** — if two tasks both try `PENDING → PROCESSING`, the second one hits the lock, sees `PROCESSING`, and raises. No silent corruption.

```python
VALID_TRANSITIONS = {
    'pending':    ['processing'],
    'processing': ['completed', 'failed'],
    'completed':  [],   # terminal
    'failed':     [],   # terminal
}

def transition_to(self, new_status):
    if new_status not in VALID_TRANSITIONS[self.status]:
        raise ValueError(f"Illegal transition: {self.status} → {new_status}")
    self.status = new_status
    self.save(update_fields=['status', 'updated_at'])
```

---

## 5. Catching what an AI assistant got wrong

An AI coding assistant suggested this for calculating merchant balance:

```python
#  What the AI suggested
entries = list(LedgerEntry.objects.filter(merchant_id=merchant_id))
balance = sum(e.amount_paise for e in entries)
```

This looks fine at a glance, but has three real problems:

1. **Loads every row into memory** — a merchant with 100k transactions fetches 100k objects just to add them up.
2. **Race condition** — between reading the rows and summing them in Python, another process could create new entries. The total is stale.
3. **Doesn't scale** — as transaction history grows, every balance check gets slower.

```python
#  What's actually used
def get_total_balance(self):
    result = self.ledger_entries.aggregate(total=Sum('amount_paise'))
    return result['total'] or 0
```

The database computes the sum in a single query. One row comes back. It's fast, atomic, and works correctly under the `select_for_update()` lock used in payout creation.

---

## Summary

| Decision | Mechanism | Guarantee |
|---|---|---|
| Ledger-based accounting | Immutable `LedgerEntry` rows, sum on demand | Balance is always accurate; no denormalization drift |
| Row-level locking | `SELECT FOR UPDATE` inside `transaction.atomic()` | Two concurrent requests cannot overdraft the account |
| Idempotency keys | UUID per request, response cached for 24h | Retries are safe; only one payout created per request |
| State machine | `transition_to()` with an allowed-transitions map | Payouts cannot move backward or skip states |
| Database aggregation | `aggregate(Sum(...))` instead of Python sum | Balance queries are O(log n) and race-condition-free |

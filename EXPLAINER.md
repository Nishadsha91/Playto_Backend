# This document explains the five core design decisions I made to ensure this system safely handles financial payouts.

---

## System Overview

Playto is a payout engine that allows merchants to withdraw funds safely.  
The system ensures financial correctness through:

- Ledger-based accounting
- Database row locking for concurrency safety
- Idempotency keys for safe retries
- A strict payout state machine
- Asynchronous payout processing

---

## 1. The Ledger

**Balance calculation query:**

```python
def get_total_balance(self):
    result = self.ledger_entries.aggregate(total=Sum('amount_paise'))
    return result['total'] or 0

def get_held_balance(self):
    result = self.payouts.filter(
        status__in=[Payout.PENDING, Payout.PROCESSING]
    ).aggregate(total=Sum('amount_paise'))
    return result['total'] or 0

def get_available_balance(self):
    return self.get_total_balance() - self.get_held_balance()
```
**Why I designed it this way:**

A simple approach would be storing a balance column on the Merchant model.
However, this approach becomes unsafe when multiple transactions happen concurrently or when a request fails during an update.

To avoid this, I implemented a ledger-based accounting system.

Every credit and debit is stored as an immutable LedgerEntry. The merchant balance is calculated by aggregating those entries instead of storing a single balance value.

This design provides several guarantees:

- **Consistency:** Balance is always derived from the full transaction history.
- **Auditability:** Every change to the balance is permanently recorded.
- **Failure safety:** If a transaction fails mid-operation, the ledger still preserves the correct history.

All monetary values are stored as paise (integers) instead of floating-point numbers to avoid precision errors in financial calculations.

Example: ```₹100.01 → 10001 paise```

---

## 2. The Lock

**The exact code that prevents overdrawing:**

```python
def create_payout(merchant_id, amount_paise, bank_account_id, idempotency_key=''):
    with transaction.atomic():
        # This is the lock. Everything after this line is serialised per merchant.
        merchant = Merchant.objects.select_for_update().get(id=merchant_id)

        # Also lock all pending/processing payouts so their amounts can't change
        locked_pending = list(
            Payout.objects.select_for_update().filter(
                merchant=merchant,
                status__in=[Payout.PENDING, Payout.PROCESSING],
            )
        )

        total_balance = (
            LedgerEntry.objects
            .filter(merchant=merchant)
            .aggregate(total=Sum('amount_paise'))['total'] or 0
        )

        held_balance = sum(p.amount_paise for p in locked_pending)
        available = total_balance - held_balance

        if amount_paise > available:
            raise ValueError(
                f"Insufficient balance. "
                f"Available: {available} paise, Requested: {amount_paise} paise."
            )

        payout = Payout.objects.create(
            merchant=merchant,
            bank_account=bank_account,
            amount_paise=amount_paise,
            status=Payout.PENDING,
            idempotency_key=idempotency_key,
        )
        return payout
```

**The database primitive it relies on:**

`SELECT FOR UPDATE` — a row-level exclusive lock in PostgreSQL. When one transaction holds it, every other transaction trying to acquire the same lock on the same row will block until the first one commits or rolls back.

The reason this has to be a database lock and not a Python-level check is timing. A Python check reads the balance and then, separately, writes the payout. There's a gap between those two operations. In a concurrent system, another request can slip into that gap — it reads the same balance, passes the same check, and creates its own payout. Both go through. You've paid out more than the merchant has.

`select_for_update()` closes that gap. The balance check and the payout creation happen inside a single atomic transaction, and no other process can read or modify the merchant row until that transaction finishes. The validation is never stale.

---

## 3. The Idempotency

**How the system knows it has seen a key before:**

Each `Idempotency-Key` header value (a UUID sent by the client) is stored in an `IdempotencyKey` table alongside the merchant, the full response body, the HTTP status code, and an expiry 24 hours out. There's a `unique_together` constraint on `(merchant, key)` so the database itself enforces uniqueness.

When a request arrives with a key, the first thing the view does — before deserializing, before validating, before touching any payout logic — is query this table:

```python
idempotency_key = request.headers.get('Idempotency-Key', '').strip()

if idempotency_key:
    cached_body, cached_status = get_idempotency_response(merchant, idempotency_key)
    if cached_body is not None:
        return Response(cached_body, status=cached_status)
```

If there's a match, the cached response goes straight back out. No payout is created. No side effects happen.

After a new payout is successfully created, the response is saved:

```python
if idempotency_key:
    save_idempotency_key(merchant, idempotency_key, response_data, response_status_code)
```

**What happens if the first request is still in flight when the second arrives:**

The key is saved to the database right after the payout is created — still within the same request cycle, before the response is sent back to the client. So if the network drops the response after the server has finished processing, the key is already stored.

When the client retries, the key lookup finds it immediately and returns the cached response. The retry sees the payout as `PENDING` — which is accurate, because it is. If the payout later completes, the client can poll the payout status endpoint and see `COMPLETED`. The important guarantee is that only one payout was ever created.

The one edge case worth noting: if the server crashes after creating the payout but before saving the idempotency key, the retry would create a second payout. That window is very small, but it exists. A stricter system would save the key and create the payout in the same transaction, using the key as a payout field. I noted this as a known tradeoff.

---

## 4. The State Machine

**Where `FAILED → COMPLETED` (and every other illegal transition) is blocked:**

```python
VALID_TRANSITIONS = {
    Payout.PENDING:    [Payout.PROCESSING],
    Payout.PROCESSING: [Payout.COMPLETED, Payout.FAILED],
    Payout.COMPLETED:  [],
    Payout.FAILED:     [],
}

def transition_to(self, new_status):
    allowed = self.VALID_TRANSITIONS.get(self.status, [])
    if new_status not in allowed:
        raise ValueError(
            f"Illegal state transition: '{self.status}' → '{new_status}'. "
            f"Allowed from '{self.status}': {allowed}"
        )
    self.status = new_status
    self.save(update_fields=['status', 'updated_at'])
```

`COMPLETED` and `FAILED` map to empty lists. Any call to `transition_to()` from either of those states raises immediately, regardless of what the target state is.

This check lives at the model level, not in the view or the task. That means it's enforced no matter what calls it — an API request, a Celery task, a management command, a manual Django shell operation. There's no way to change a payout status that bypasses this.

The task also has a second guard before it even attempts a transition:

```python
payout = Payout.objects.select_for_update().get(id=payout_id)

if payout.status != Payout.PENDING:
    logger.info(f"Payout {payout_id} is already '{payout.status}', skipping.")
    return
```

This handles the case where the same task is queued twice. The `select_for_update()` lock means only one instance runs the check at a time. The second one unblocks, sees a non-PENDING status, and exits cleanly without trying to transition.

---

## 5. The AI Audit

**What the AI suggested:**

```python
# AI-generated balance calculation
def get_merchant_balance(merchant_id):
    entries = list(LedgerEntry.objects.filter(merchant_id=merchant_id))
    balance = sum(e.amount_paise for e in entries)
    return balance
```

This was suggested as the implementation for `get_merchant_balance`. It looks reasonable. It's also wrong in a few ways that matter.

**What I caught:**

First, it loads every ledger entry into Python memory. A merchant who has processed thousands of payouts has thousands of rows. This query fetches all of them, constructs Django model objects for each, and then adds up one field. The database already knows how to sum a column in a single operation — this makes it do the hard work and then throws most of it away.

Second, and more importantly, the sum happens in Python *after* the database read. In a concurrent system, another process could insert a new ledger entry between the `filter()` and the `sum()`. The total you just calculated is already stale. This isn't theoretical — it's exactly the kind of thing that happens under load.

Third, there's no mention of locking. Even if you wrapped this in a transaction, reading all the rows and summing them in Python isn't the same as a database-level aggregate inside a `select_for_update()` block. The isolation guarantees are different.

**What I replaced it with:**

```python
def get_total_balance(self):
    result = self.ledger_entries.aggregate(total=Sum('amount_paise'))
    return result['total'] or 0
```

One SQL query. The database returns a single number. When this is called from inside `create_payout()`, it's already executing within the `select_for_update()` transaction, so the read is consistent and no other writer can interfere. It also stays fast regardless of how many ledger entries accumulate — the `(merchant_id, created_at)` index means the database doesn't scan the full table.

The AI-generated implementation calculated balances in Python after fetching all ledger entries. While this works for small datasets, it becomes inefficient and unsafe in production environments.

To solve this, I replaced it with a database aggregation query using `SUM`, which is faster, more scalable, and safer under concurrent transactions.

---

## Additional Details

The sections above explain the key architectural decisions behind the payout engine.

For setup instructions, API usage, deployment details, and project structure, please refer to the **README.md** file in the repository.

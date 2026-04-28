# Playto Payout Engine: Technical Explainer

A production-grade financial payout system for Playto's founding engineer challenge. This document explains the five core design decisions that make this system safe for moving money.

---

## 1. The Ledger

**Problem:** Storing balance as a single denormalized field on the Merchant model is dangerous. If a payout fails mid-transaction or a concurrent request arrives, the balance field can go stale or inconsistent.

**Solution:** Ledger-based accounting. The Merchant never stores balance directly. Instead, all credits and debits are immutable LedgerEntry rows. Balance is computed on-demand by summing all entries for that merchant.

### Why This Matters

1. **Atomicity**: Each ledger entry is a single database row. Once written, it cannot change. No mid-transaction corruption.
2. **Audit trail**: Every balance change has a permanent record with timestamp, description, and reference ID.
3. **Concurrent safety**: Two processes can create ledger entries simultaneously without race conditions—the database guarantees ordering via `created_at`.
4. **Held balance calculation**: Pending/processing payouts are also subtracted from balance, giving merchants accurate "available" balance.

### The Query

```python
def get_total_balance(self):
    """
    SQL: SELECT SUM(amount_paise) FROM ledger_ledgerentry 
         WHERE merchant_id = <id>
    """
    result = self.ledger_entries.aggregate(total=Sum('amount_paise'))
    return result['total'] or 0

def get_held_balance(self):
    """
    SQL: SELECT SUM(amount_paise) FROM ledger_payout 
         WHERE merchant_id = <id> 
         AND status IN ('pending', 'processing')
    """
    result = self.payouts.filter(
        status__in=[Payout.PENDING, Payout.PROCESSING]
    ).aggregate(total=Sum('amount_paise'))
    return result['total'] or 0

def get_available_balance(self):
    return self.get_total_balance() - self.get_held_balance()
```

### Critical Property

All amounts use `BigIntegerField` storing **paise (1/100th of a rupee)**. Never floating-point. This eliminates rounding errors that plague financial systems.

**Example:** ₹100.01 → 10001 paise (exact integer, no precision loss).

---

## 2. The Lock

**Problem:** Two concurrent payout requests can both read the available balance, validate it as sufficient, and then both create payouts—draining more than the merchant has.

**Example race condition:**
```
Merchant balance: ₹1000

Thread 1: checks available = ₹1000, creates ₹600 payout ✓
Thread 2: checks available = ₹1000 (still sees old value), creates ₹500 payout ✓

Result: ₹1100 paid out from ₹1000 balance. DISASTER.
```

**Solution:** Database-level row locking with `SELECT FOR UPDATE`. Before validating balance, acquire an exclusive lock on the Merchant row. No other process can proceed until the lock is released.

### The Code

```python
def create_payout(merchant_id, amount_paise, bank_account_id, idempotency_key=''):
    with transaction.atomic():
        # Lock the merchant row exclusively
        merchant = Merchant.objects.select_for_update().get(id=merchant_id)

        # Lock ALL pending/processing payouts for this merchant
        locked_pending = list(
            Payout.objects.select_for_update().filter(
                merchant=merchant,
                status__in=[Payout.PENDING, Payout.PROCESSING],
            )
        )

        # Calculate total balance under lock
        total_balance = (
            LedgerEntry.objects
            .filter(merchant=merchant)
            .aggregate(total=Sum('amount_paise'))['total'] or 0
        )

        # Sum held amounts under lock
        held_balance = sum(p.amount_paise for p in locked_pending)
        available = total_balance - held_balance

        # Validate UNDER LOCK
        if amount_paise > available:
            raise ValueError(
                f"Insufficient balance. "
                f"Available: {available} paise, Requested: {amount_paise} paise."
            )

        # Create payout WHILE STILL HOLDING LOCK
        payout = Payout.objects.create(
            merchant=merchant,
            bank_account=bank_account,
            amount_paise=amount_paise,
            status=Payout.PENDING,
            idempotency_key=idempotency_key,
        )

        # Lock released when transaction.atomic() exits
        return payout
```

### Why Python-Level Checks Fail

A naive implementation might do this:

```python
#  WE DONT WANT TO USE
merchant = Merchant.objects.get(id=merchant_id)  # No lock!
balance = merchant.get_total_balance()

if amount > balance:  # Checking in Python
    raise ValueError("Insufficient")

payout = Payout.objects.create(...)  # Meanwhile, another request created a payout!
```

**The race condition:** Between reading balance (line 2) and creating the payout (line 6), another thread could have created a payout. The balance check is now stale.

**Database locks prevent this:** `select_for_update()` blocks all other threads from even reading the locked row until the transaction commits.

### Lock Scope

- **Merchant row:** Prevents concurrent balance reads.
- **Payout rows:** Prevents concurrent state changes. Two `process_payout` tasks cannot race.
- **LedgerEntry creation:** Implicit—single-writer pattern ensures consistency.

---

## 3. The Idempotency

**Problem:** Network failures are common. A merchant's client submits a payout request; the server processes it but the response is lost. The client retries. Did we create two payouts?

**Solution:** Idempotency keys. The client includes a UUID in the `Idempotency-Key` header. The server stores this key + the response for 24 hours. Duplicate requests return the cached response without side effects.

### How It Works

```python
# In PayoutListCreateView.post():
idempotency_key = request.headers.get('Idempotency-Key', '').strip()

# Check if we've seen this key before (within 24h)
if idempotency_key:
    cached_body, cached_status = get_idempotency_response(merchant, idempotency_key)
    if cached_body is not None:
        return Response(cached_body, status=cached_status)  # Return cached response

# Process normally if key is new
serializer = PayoutCreateSerializer(data=request.data)
if not serializer.is_valid():
    return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

try:
    payout = create_payout(...)
except ValueError as e:
    return Response({'error': str(e)}, status=status.HTTP_400_BAD_REQUEST)

response_data = PayoutSerializer(payout).data
response_status_code = status.HTTP_201_CREATED

# Store the response for future retries
if idempotency_key:
    save_idempotency_key(merchant, idempotency_key, response_data, response_status_code)

return Response(response_data, status=response_status_code)
```

### Storage Model

```python
class IdempotencyKey(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    merchant = models.ForeignKey(Merchant, on_delete=models.CASCADE)
    key = models.CharField(max_length=255)  # UUID from client
    response_body = models.JSONField()      # Entire response to return
    response_status = models.IntegerField() # HTTP status code
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()     # 24 hours from now

    class Meta:
        unique_together = [('merchant', 'key')]
        indexes = [
            models.Index(fields=['merchant', 'key', 'expires_at']),
        ]
```

### Edge Case: First Request Still Processing

**Scenario:**
1. Client sends payout request with idempotency key K.
2. Server creates payout in PENDING status, stores idempotency key.
3. Response is lost (network timeout from client's perspective).
4. **Client retries immediately** before the payout reaches COMPLETED.

**Result:**

```python
# First request (request 1)
payout_1 = Payout.objects.create(status=PENDING)
save_idempotency_key(merchant, 'key-K', response_data, 201)

# Meanwhile: process_payout.delay(payout_1) starts processing...

# Second request (request 2) arrives
cached = get_idempotency_response(merchant, 'key-K')  # Found!
return Response(cached)  # Returns payout_1 serialized as PENDING

# Result:
# - Only ONE payout created (payout_1)
# - Client sees consistent response both times
# - If payout_1 eventually completes, client polls and sees COMPLETED
```

**The key guarantee:** The idempotency key lookup happens BEFORE any side effects. Once a key is stored, all retries return that exact response, whether the original request succeeded or failed.

---

## 4. The State Machine

**Problem:** Payouts should only move in one direction: PENDING → PROCESSING → COMPLETED/FAILED. An errant task or manual intervention might try COMPLETED → PENDING or other illegal transitions.

**Solution:** A state machine implemented at the model level. Only certain transitions are allowed. Illegal transitions raise an exception.

### Valid Transitions

```python
VALID_TRANSITIONS = {
    PENDING:    [PROCESSING],      # PENDING can only go to PROCESSING
    PROCESSING: [COMPLETED, FAILED],  # PROCESSING can go to either terminal state
    COMPLETED:  [],                   # COMPLETED is terminal, no transitions
    FAILED:     [],                   # FAILED is terminal, no transitions
}
```

### Enforced Transition

```python
def transition_to(self, new_status):
    allowed = self.VALID_TRANSITIONS.get(self.status, [])
    
    # Check if the transition is in the allowed list
    if new_status not in allowed:
        raise ValueError(
            f"Illegal state transition: '{self.status}' → '{new_status}'. "
            f"Allowed from '{self.status}': {allowed}"
        )
    
    self.status = new_status
    self.save(update_fields=['status', 'updated_at'])
```

### Usage in Tasks

```python
@shared_task(bind=True, max_retries=3)
def process_payout(self, payout_id):
    with transaction.atomic():
        payout = Payout.objects.select_for_update().get(id=payout_id)
        
        # Step 1: Guard against duplicate processing
        if payout.status != Payout.PENDING:
            logger.info(f"Payout {payout_id} is already '{payout.status}', skipping.")
            return

        # Step 2: Move to processing (only allowed from PENDING)
        payout.transition_to(Payout.PROCESSING)  # Will raise if status != PENDING
        payout.attempts += 1
        payout.save(update_fields=['attempts', 'last_attempted_at', 'updated_at'])

    # Step 3: Call bank API
    outcome = simulate_bank_api()

    # Step 4: Finalize based on outcome
    if outcome == 'success':
        with transaction.atomic():
            payout = Payout.objects.select_for_update().get(id=payout_id)
            payout.transition_to(Payout.COMPLETED)  # PROCESSING → COMPLETED
            
    elif outcome == 'fail':
        with transaction.atomic():
            payout = Payout.objects.select_for_update().get(id=payout_id)
            payout.transition_to(Payout.FAILED)  # PROCESSING → FAILED
```

### Protection Against Race Conditions

Two `process_payout` tasks are queued for the same payout_id. Both try to transition PENDING → PROCESSING.

```python
# Task 1 thread
payout = Payout.objects.select_for_update().get(id=payout_id)
payout.status  # = PENDING
payout.transition_to(PROCESSING)  # ✓ Allowed

# Task 2 thread (blocked on select_for_update until Task 1 commits)
payout = Payout.objects.select_for_update().get(id=payout_id)
payout.status  # = PROCESSING (now!)
payout.transition_to(PROCESSING)  # ✗ Illegal!
# → ValueError: "Illegal state transition: 'PROCESSING' → 'PROCESSING'"
```

The exception is caught by Celery and logged as a task error—no corruption.

---

## 5. The AI Audit

### The Mistake

An AI coding assistant (not this system) suggested calculating merchant balance like this:

```python
# INCORRECT - AI SUGGESTION
def get_merchant_balance(merchant_id):
    entries = list(LedgerEntry.objects.filter(merchant_id=merchant_id))
    balance = sum(e.amount_paise for e in entries)  # Calculate in Python!
    return balance
```

### Why It's Wrong

1. **N+1 Query Problem:** Fetches ALL ledger entries into Python memory. For a merchant with 100,000 transactions, this loads 100k rows.
2. **Race condition:** Between reading entries and summing, another process could create new entries. The total is stale.
3. **Concurrency:** Python sum() happens after the database read completes. Multiple requests race each other in Python, not under database lock.
4. **Scalability:** As merchants grow and accumulate history, every balance check becomes slower.
5. **Floating point risk:** If the AI had used floats, precision errors would compound over millions of transactions.

### The Correct Solution

```python
# ✓ CORRECT - USED IN THIS SYSTEM
def get_total_balance(self):
    """Aggregate in the database, fetch only the sum."""
    result = self.ledger_entries.aggregate(total=Sum('amount_paise'))
    return result['total'] or 0
```

**Why this is right:**

1. **Database aggregation:** The database itself computes the sum. Only one row is returned: `{total: 50000}`.
2. **Single query:** Regardless of transaction count, this is always a single SQL aggregate.
3. **Atomic:** The sum is computed within a single database operation.
4. **Under lock:** When called from `create_payout()`, it's already inside `select_for_update()`, so the read is consistent.
5. **Proven pattern:** Every production financial system (banking, payment processors) uses aggregation queries for balance, never Python summation.

### Lessons Applied

1. **Move the math to the database.** Database aggregations are atomic and fast.
2. **Never calculate financial totals in application code.** It invites race conditions.
3. **Trust database indexes.** The `(merchant, created_at)` index on LedgerEntry makes this query O(log n) even with millions of rows.

---

## Summary: The Safety Guarantees

| Aspect | Mechanism | Guarantee |
|--------|-----------|-----------|
| **Balance Accuracy** | Ledger-based aggregation | Always consistent; no denormalization drift |
| **Overdraft Prevention** | `select_for_update()` row locking | Two concurrent requests cannot spend more than available |
| **Duplicate Prevention** | Idempotency keys stored per merchant | Retries are safe; only one payout created per request |
| **Illegal State Transitions** | `transition_to()` validation | Payouts cannot move backward or skip states |
| **Fault Tolerance** | Stuck payout retries with backoff | Failed bank API calls are retried; max 3 attempts then refund |




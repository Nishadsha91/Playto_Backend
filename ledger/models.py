import uuid
from django.db import models
from django.contrib.auth.models import User
from django.db.models import Sum
from django.utils import timezone


class Merchant(models.Model):
    """
    One Merchant per Django User.
    Balance is NEVER stored here — always computed from LedgerEntry rows.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name='merchant')
    name = models.CharField(max_length=255)
    email = models.EmailField(unique=True)
    created_at = models.DateTimeField(auto_now_add=True)

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

    def __str__(self):
        return self.name


class LedgerEntry(models.Model):

    CREDIT = 'credit'
    DEBIT = 'debit'
    ENTRY_TYPE_CHOICES = [
        (CREDIT, 'Credit'),
        (DEBIT, 'Debit'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    merchant = models.ForeignKey(Merchant, on_delete=models.PROTECT, related_name='ledger_entries' )
    amount_paise = models.BigIntegerField()
    entry_type = models.CharField(max_length=10, choices=ENTRY_TYPE_CHOICES)
    description = models.CharField(max_length=500)
    reference_id = models.CharField(max_length=255, blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['merchant', 'created_at']),
        ]

    def __str__(self):
        return f"{self.entry_type} {self.amount_paise} paise for {self.merchant.name}"


class BankAccount(models.Model):

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    merchant = models.ForeignKey( Merchant, on_delete=models.PROTECT, related_name='bank_accounts')
    account_number = models.CharField(max_length=20)
    ifsc_code = models.CharField(max_length=11)
    account_holder_name = models.CharField(max_length=255)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.account_holder_name} — {self.account_number}"


class Payout(models.Model):

    PENDING = 'pending'
    PROCESSING = 'processing'
    COMPLETED = 'completed'
    FAILED = 'failed'

    STATUS_CHOICES = [
        (PENDING, 'Pending'),
        (PROCESSING, 'Processing'),
        (COMPLETED, 'Completed'),
        (FAILED, 'Failed'),
    ]

    VALID_TRANSITIONS = {
        PENDING:    [PROCESSING],
        PROCESSING: [COMPLETED, FAILED],
        COMPLETED:  [],   # terminal
        FAILED:     [],   # terminal
    }

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    merchant = models.ForeignKey( Merchant, on_delete=models.PROTECT, related_name='payouts')
    bank_account = models.ForeignKey( BankAccount, on_delete=models.PROTECT, related_name='payouts')
    amount_paise = models.BigIntegerField()
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=PENDING)
    idempotency_key = models.CharField(max_length=255, blank=True, default='')
    attempts = models.IntegerField(default=0)
    last_attempted_at = models.DateTimeField(null=True, blank=True)
    failure_reason = models.TextField(blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']

    def transition_to(self, new_status):

        allowed = self.VALID_TRANSITIONS.get(self.status, [])
        if new_status not in allowed:
            raise ValueError(
                f"Illegal state transition: '{self.status}' → '{new_status}'. "
                f"Allowed from '{self.status}': {allowed}"
            )
        self.status = new_status
        self.save(update_fields=['status', 'updated_at'])

    def __str__(self):
        return f"Payout {self.id} — {self.amount_paise} paise — {self.status}"


class IdempotencyKey(models.Model):

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    merchant = models.ForeignKey( Merchant, on_delete=models.CASCADE, related_name='idempotency_keys' )
    key = models.CharField(max_length=255)
    response_body = models.JSONField()
    response_status = models.IntegerField()
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()

    class Meta:
        unique_together = [('merchant', 'key')]
        indexes = [
            models.Index(fields=['merchant', 'key', 'expires_at']),
        ]

    def __str__(self):
        return f"IdempotencyKey '{self.key}' for {self.merchant.name}"
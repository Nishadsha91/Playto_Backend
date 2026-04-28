from django.db import transaction, IntegrityError
from django.db.models import Sum
from django.utils import timezone
from datetime import timedelta

from .models import Merchant, Payout, LedgerEntry, BankAccount, IdempotencyKey


def create_payout(merchant_id, amount_paise, bank_account_id, idempotency_key=''):

    with transaction.atomic():
        merchant = Merchant.objects.select_for_update().get(id=merchant_id)

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

        if amount_paise <= 0:
            raise ValueError("Payout amount must be greater than zero.")

        if amount_paise > available:
            raise ValueError(
                f"Insufficient balance. "
                f"Available: {available} paise, Requested: {amount_paise} paise."
            )

        try:
            bank_account = BankAccount.objects.get(
                id=bank_account_id,
                merchant=merchant,
                is_active=True,
            )
        except BankAccount.DoesNotExist:
            raise ValueError("Bank account not found or not active.")

      
        if idempotency_key:
            existing = IdempotencyKey.objects.filter( merchant=merchant, key=idempotency_key, expires_at__gt=timezone.now(),).first()
            if existing:
                try:
                    return Payout.objects.get(
                        merchant=merchant,
                        idempotency_key=idempotency_key,
                    )
                except Payout.DoesNotExist:
                    pass  

        payout = Payout.objects.create(
            merchant=merchant,
            bank_account=bank_account,
            amount_paise=amount_paise,
            status=Payout.PENDING,
            idempotency_key=idempotency_key,
        )

        return payout


def save_idempotency_key(merchant, key, response_body, response_status):

    expires_at = timezone.now() + timedelta(hours=24)

    obj, created = IdempotencyKey.objects.get_or_create(
        merchant=merchant,
        key=key,
        defaults={
            'response_body': response_body,
            'response_status': response_status,
            'expires_at': expires_at,
        }
    )
    return obj


def get_idempotency_response(merchant, key):

    now = timezone.now()
    try:
        record = IdempotencyKey.objects.get(
            merchant=merchant,
            key=key,
            expires_at__gt=now,
        )
        return record.response_body, record.response_status
    except IdempotencyKey.DoesNotExist:
        return None, None
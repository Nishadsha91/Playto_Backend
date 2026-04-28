import random
import logging
from celery import shared_task
from django.db import transaction
from django.utils import timezone
from datetime import timedelta

from .models import Payout, LedgerEntry

logger = logging.getLogger(__name__)


def simulate_bank_api():

    roll = random.random()
    if roll < 0.70:
        return 'success'
    elif roll < 0.90:
        return 'fail'
    else:
        return 'hang'


@shared_task(bind=True, max_retries=3)
def process_payout(self, payout_id):

    logger.info(f"process_payout starting: {payout_id}")

    try:
        with transaction.atomic():
            payout = Payout.objects.select_for_update().get(id=payout_id)
            #step 1
            if payout.status != Payout.PENDING:

                logger.info(f"Payout {payout_id} is already '{payout.status}', skipping.")
                return

            payout.transition_to(Payout.PROCESSING)
            payout.attempts += 1
            payout.last_attempted_at = timezone.now()
            payout.save(update_fields=['attempts', 'last_attempted_at', 'updated_at'])

    except Payout.DoesNotExist:
        logger.error(f"Payout {payout_id} not found.")
        return

    # Step 2 
    outcome = simulate_bank_api()
    logger.info(f"Bank API outcome for payout {payout_id}: {outcome}")

    # Step 3 
    
    if outcome == 'success':
        with transaction.atomic():
            payout = Payout.objects.select_for_update().get(id=payout_id)

            if payout.status != Payout.PROCESSING:
                logger.warning(f"Payout {payout_id} no longer in processing on success path.")
                return

          
            LedgerEntry.objects.create(
                merchant=payout.merchant,
                amount_paise=-payout.amount_paise,  
                entry_type=LedgerEntry.DEBIT,
                description=f"Payout completed — {payout.bank_account.account_number}",
                reference_id=str(payout.id),
            )
            payout.transition_to(Payout.COMPLETED)

        logger.info(f"Payout {payout_id} completed successfully.")

    elif outcome == 'fail':
        with transaction.atomic():
            payout = Payout.objects.select_for_update().get(id=payout_id)

            if payout.status != Payout.PROCESSING:
                logger.warning(f"Payout {payout_id} no longer in processing on fail path.")
                return

            payout.failure_reason = 'Bank rejected the transfer.'
            payout.save(update_fields=['failure_reason', 'updated_at'])

    
            LedgerEntry.objects.create(
                merchant=payout.merchant,
                amount_paise=payout.amount_paise,    
                entry_type=LedgerEntry.CREDIT,
                description=f"Refund for failed payout {payout.id}",
                reference_id=str(payout.id),
            )
            payout.transition_to(Payout.FAILED)

        logger.info(f"Payout {payout_id} failed. Funds refunded.")

    elif outcome == 'hang':
        countdown = 2 ** self.request.retries
        logger.warning(
            f"Payout {payout_id} hung. "
            f"Retry {self.request.retries + 1}/3 in {countdown}s."
        )
        try:
            raise self.retry(countdown=countdown, exc=Exception('Bank API timed out'))
        except self.MaxRetriesExceededError:
            logger.error(f"Payout {payout_id} exceeded max retries. Marking failed.")
            with transaction.atomic():
                payout = Payout.objects.select_for_update().get(id=payout_id)
                if payout.status == Payout.PROCESSING:
                    payout.failure_reason = 'Max retries exceeded. Bank did not respond.'
                    payout.save(update_fields=['failure_reason', 'updated_at'])
                    LedgerEntry.objects.create(
                        merchant=payout.merchant,
                        amount_paise=payout.amount_paise,
                        entry_type=LedgerEntry.CREDIT,
                        description=f"Refund after max retries — payout {payout.id}",
                        reference_id=str(payout.id),
                    )
                    payout.transition_to(Payout.FAILED)


@shared_task
def retry_stuck_payouts():

    cutoff_time = timezone.now() - timedelta(seconds=30)

    stuck_ids = list(
        Payout.objects.filter(
            status=Payout.PROCESSING,
            last_attempted_at__lt=cutoff_time,
            attempts__lt=3,
        ).values_list('id', flat=True)
    )

    for payout_id in stuck_ids:
        try:
            with transaction.atomic():

                payout = Payout.objects.select_for_update().get(id=payout_id)

                if payout.status != Payout.PROCESSING:
                    continue
                if payout.last_attempted_at and payout.last_attempted_at >= cutoff_time:
                    continue

                payout.status = Payout.PENDING
                payout.save(update_fields=['status', 'updated_at'])

            process_payout.delay(str(payout_id))
            logger.warning(f"Re-queued stuck payout {payout_id}")

        except Payout.DoesNotExist:
            pass
        except Exception as e:
            logger.error(f"Error re-queuing payout {payout_id}: {e}")
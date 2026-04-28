from django.test import TransactionTestCase
from django.contrib.auth.models import User
from rest_framework.test import APIClient
from concurrent.futures import ThreadPoolExecutor, as_completed

from .models import Merchant, LedgerEntry, BankAccount, Payout


class PayoutConcurrencyTest(TransactionTestCase):

    def setUp(self):
        self.user = User.objects.create_user(username='concurrency_merchant', password='testpass')
        self.merchant = Merchant.objects.create(
            user=self.user,
            name='Concurrency Test Merchant',
            email='concurrency@test.com',
        )
        self.bank_account = BankAccount.objects.create(
            merchant=self.merchant,
            account_number='111122223333',
            ifsc_code='HDFC0001234',
            account_holder_name='Concurrency Test',
        )

        LedgerEntry.objects.create(
            merchant=self.merchant,
            amount_paise=10000,
            entry_type=LedgerEntry.CREDIT,
            description='Initial funding',
        )

    def _make_payout_request(self, amount_paise):

        client = APIClient()
        client.force_authenticate(user=self.user)
        return client.post('/api/v1/payouts/', {
            'amount_paise': amount_paise,
            'bank_account_id': str(self.bank_account.id),
        })

    def test_concurrent_overdraft_prevented(self):
        
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [
                executor.submit(self._make_payout_request, 6000)
                for _ in range(2)
            ]
            responses = [f.result() for f in as_completed(futures)]

        status_codes = sorted([r.status_code for r in responses])

        self.assertCountEqual(status_codes, [201, 400])
        self.assertIn(201, status_codes)
        self.assertIn(400, status_codes)

        self.assertEqual(Payout.objects.filter(merchant=self.merchant).count(), 1)

        payout = Payout.objects.get(merchant=self.merchant)
        self.assertEqual(payout.amount_paise, 6000)

        self.assertEqual(self.merchant.get_available_balance(), 4000)


class PayoutIdempotencyTest(TransactionTestCase):

    def setUp(self):
        self.user = User.objects.create_user(username='idempotency_merchant', password='testpass')
        self.merchant = Merchant.objects.create(
            user=self.user,
            name='Idempotency Test Merchant',
            email='idempotency@test.com',
        )
        self.bank_account = BankAccount.objects.create(
            merchant=self.merchant,
            account_number='999988887777',
            ifsc_code='ICIC0004321',
            account_holder_name='Idempotency Test',
        )
        LedgerEntry.objects.create(
            merchant=self.merchant,
            amount_paise=20000,
            entry_type=LedgerEntry.CREDIT,
            description='Initial funding',
        )
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

    def test_same_key_returns_same_response(self):

        payload = {
            'amount_paise': 5000,
            'bank_account_id': str(self.bank_account.id),
        }
        headers = {'HTTP_IDEMPOTENCY_KEY': 'unique-key-abc-123'}

        response1 = self.client.post('/api/v1/payouts/', payload, **headers)
        response2 = self.client.post('/api/v1/payouts/', payload, **headers)

        self.assertEqual(response1.status_code, 201)
        self.assertEqual(response2.status_code, 201)

        self.assertEqual(response1.data['id'], response2.data['id'])

        self.assertEqual(Payout.objects.filter(merchant=self.merchant).count(), 1)

    def test_different_keys_create_separate_payouts(self):

        payload = {
            'amount_paise': 3000,
            'bank_account_id': str(self.bank_account.id),
        }

        r1 = self.client.post('/api/v1/payouts/', payload, HTTP_IDEMPOTENCY_KEY='key-AAA')
        r2 = self.client.post('/api/v1/payouts/', payload, HTTP_IDEMPOTENCY_KEY='key-BBB')

        self.assertEqual(r1.status_code, 201)
        self.assertEqual(r2.status_code, 201)
        self.assertNotEqual(r1.data['id'], r2.data['id'])
        self.assertEqual(Payout.objects.filter(merchant=self.merchant).count(), 2)


class StateMachineTest(TransactionTestCase):

    def setUp(self):
        self.user = User.objects.create_user(username='state_merchant', password='testpass')
        self.merchant = Merchant.objects.create(
            user=self.user,
            name='State Test Merchant',
            email='state@test.com',
        )
        self.bank_account = BankAccount.objects.create(
            merchant=self.merchant,
            account_number='555566667777',
            ifsc_code='SBIN0009876',
            account_holder_name='State Test',
        )

    def _make_payout(self, status):
        return Payout.objects.create(
            merchant=self.merchant,
            bank_account=self.bank_account,
            amount_paise=1000,
            status=status,
        )

    def test_completed_to_pending_blocked(self):
        payout = self._make_payout(Payout.COMPLETED)
        with self.assertRaises(ValueError):
            payout.transition_to(Payout.PENDING)

    def test_failed_to_completed_blocked(self):
        payout = self._make_payout(Payout.FAILED)
        with self.assertRaises(ValueError):
            payout.transition_to(Payout.COMPLETED)

    def test_completed_to_processing_blocked(self):
        payout = self._make_payout(Payout.COMPLETED)
        with self.assertRaises(ValueError):
            payout.transition_to(Payout.PROCESSING)

    def test_valid_transitions_allowed(self):
        payout = self._make_payout(Payout.PENDING)
        payout.transition_to(Payout.PROCESSING)   
        payout.transition_to(Payout.COMPLETED)   
        self.assertEqual(payout.status, Payout.COMPLETED)
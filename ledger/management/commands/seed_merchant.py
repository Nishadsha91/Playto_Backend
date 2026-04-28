from django.core.management.base import BaseCommand
from django.contrib.auth.models import User
from ledger.models import Merchant, LedgerEntry, BankAccount
from django.db import transaction



class Command(BaseCommand):
    help = 'Creates 3 test merchants with bank accounts and credit history'

    def handle(self, *args, **kwargs):

        merchants_data = [
            {
                'username': 'merchant1',
                'password': 'testpass123',
                'name': 'Rahul Sharma Design Studio',
                'email': 'rahul@designstudio.com',
                'bank': {
                    'account_number': '1234567890123456',
                    'ifsc_code': 'HDFC0001234',
                    'account_holder_name': 'Rahul Sharma',
                },
                'credits': [
                    (150000, 'Payment from Acme Corp — Invoice #001'),
                    (200000, 'Payment from Beta LLC — Invoice #002'),
                    (75000,  'Payment from Gamma Inc — Invoice #003'),
                ],
            },
            {
                'username': 'merchant2',
                'password': 'testpass123',
                'name': 'Priya Nair Freelance Dev',
                'email': 'priya@freelancedev.com',
                'bank': {
                    'account_number': '9876543210987654',
                    'ifsc_code': 'ICIC0004321',
                    'account_holder_name': 'Priya Nair',
                },
                'credits': [
                    (300000, 'Payment from Delta Corp — Invoice #101'),
                    (125000, 'Payment from Epsilon Ltd — Invoice #102'),
                ],
            },
            {
                'username': 'merchant3',
                'password': 'testpass123',
                'name': 'Arjun Mehta Content Agency',
                'email': 'arjun@contentagency.com',
                'bank': {
                    'account_number': '1122334455667788',
                    'ifsc_code': 'SBIN0009876',
                    'account_holder_name': 'Arjun Mehta',
                },
                'credits': [
                    (500000, 'Payment from Zeta Inc — Invoice #201'),
                    (250000, 'Payment from Eta Corp — Invoice #202'),
                    (100000, 'Payment from Theta LLC — Invoice #203'),
                ],
            },
        ]

        for data in merchants_data:
            if User.objects.filter(username=data['username']).exists():
                self.stdout.write(f"Skipping {data['name']} — already exists.")
                continue

            with transaction.atomic():
                user = User.objects.create_user(
                    username=data['username'],
                    password=data['password'],
                    email=data['email'],
                )

                merchant = Merchant.objects.create(
                    user=user,
                    name=data['name'],
                    email=data['email'],
                )

                BankAccount.objects.create(
                    merchant=merchant,
                    **data['bank']
                )

            for amount, description in data['credits']:
                LedgerEntry.objects.create(
                    merchant=merchant,
                    amount_paise=amount,
                    entry_type=LedgerEntry.CREDIT,
                    description=description,
                )

            self.stdout.write(
                self.style.SUCCESS(
                    f"Created merchant: {merchant.name} | "
                    f"Balance: {merchant.get_total_balance()} paise | "
                    f"Login: {data['username']} / {data['password']}"
                )
            )

        self.stdout.write(self.style.SUCCESS("\n All merchants seeded successfully!"))


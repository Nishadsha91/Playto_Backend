from rest_framework import serializers
from .models import Merchant, LedgerEntry, BankAccount, Payout


class BankAccountSerializer(serializers.ModelSerializer):
    class Meta:
        model = BankAccount
        fields = ['id', 'account_number', 'ifsc_code', 'account_holder_name', 'is_active', 'created_at']
        read_only_fields = ['id', 'created_at']


class LedgerEntrySerializer(serializers.ModelSerializer):
    amount_rupees = serializers.SerializerMethodField()

    class Meta:
        model = LedgerEntry
        fields = ['id', 'amount_paise', 'amount_rupees', 'entry_type', 'description', 'reference_id', 'created_at']
        read_only_fields = fields

    def get_amount_rupees(self, obj):
        return obj.amount_paise / 100


class MerchantSerializer(serializers.ModelSerializer):

    available_balance = serializers.SerializerMethodField()
    held_balance = serializers.SerializerMethodField()
    total_balance = serializers.SerializerMethodField()
    available_balance_rupees = serializers.SerializerMethodField()
    held_balance_rupees = serializers.SerializerMethodField()
    total_balance_rupees = serializers.SerializerMethodField()

    class Meta:
        model = Merchant
        fields = [
            'id', 'name', 'email', 'available_balance', 'available_balance_rupees','held_balance', 
            'held_balance_rupees','total_balance', 'total_balance_rupees',
            'created_at',
        ]
        read_only_fields = fields

    def get_available_balance(self, obj):
        return obj.get_available_balance()

    def get_held_balance(self, obj):
        return obj.get_held_balance()

    def get_total_balance(self, obj):
        return obj.get_total_balance()

    def get_available_balance_rupees(self, obj):
        return obj.get_available_balance() / 100

    def get_held_balance_rupees(self, obj):
        return obj.get_held_balance() / 100

    def get_total_balance_rupees(self, obj):
        return obj.get_total_balance() / 100


class PayoutCreateSerializer(serializers.Serializer):

    amount_paise = serializers.IntegerField(min_value=1)
    bank_account_id = serializers.UUIDField()

    def validate_amount_paise(self, value):
        if value <= 0:
            raise serializers.ValidationError("Amount must be greater than 0 paise.")
        return value


class PayoutSerializer(serializers.ModelSerializer):

    bank_account = BankAccountSerializer(read_only=True)
    amount_rupees = serializers.SerializerMethodField()

    class Meta:
        model = Payout
        fields = [
            'id', 'amount_paise', 'amount_rupees', 'status', 'bank_account',
            'attempts', 'failure_reason',
            'created_at', 'updated_at',
        ]
        read_only_fields = fields

    def get_amount_rupees(self, obj):
        return obj.amount_paise / 100
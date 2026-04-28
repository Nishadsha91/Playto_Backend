from django.contrib import admin
from .models import Merchant, LedgerEntry, BankAccount, Payout, IdempotencyKey


@admin.register(Merchant)
class MerchantAdmin(admin.ModelAdmin):
    list_display = ['name', 'email', 'created_at']
    search_fields = ['name', 'email']


@admin.register(LedgerEntry)
class LedgerEntryAdmin(admin.ModelAdmin):
    list_display = ['merchant', 'entry_type', 'amount_paise', 'description', 'created_at']
    list_filter = ['entry_type', 'merchant']
    search_fields = ['merchant__name', 'description']


@admin.register(BankAccount)
class BankAccountAdmin(admin.ModelAdmin):
    list_display = ['merchant', 'account_holder_name', 'account_number', 'ifsc_code', 'is_active']
    list_filter = ['is_active']


@admin.register(Payout)
class PayoutAdmin(admin.ModelAdmin):
    list_display = ['merchant', 'amount_paise', 'status', 'attempts', 'created_at', 'updated_at']
    list_filter = ['status']
    search_fields = ['merchant__name']


@admin.register(IdempotencyKey)
class IdempotencyKeyAdmin(admin.ModelAdmin):
    list_display = ['merchant', 'key', 'response_status', 'created_at', 'expires_at']
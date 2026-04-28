from django.urls import path
from . import views

urlpatterns = [
    path('merchants/me/', views.MerchantDetailView.as_view(), name='merchant-detail'),

    path('ledger/', views.LedgerEntryListView.as_view(), name='ledger-list'),

    path('bank-accounts/', views.BankAccountListView.as_view(), name='bank-account-list'),

    path('payouts/', views.PayoutListCreateView.as_view(), name='payout-list-create'),
    path('payouts/<uuid:payout_id>/', views.PayoutDetailView.as_view(), name='payout-detail'),
]
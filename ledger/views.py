from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from rest_framework.permissions import AllowAny
import logging

from .models import Merchant, Payout
from .serializers import (
    MerchantSerializer,
    LedgerEntrySerializer,
    BankAccountSerializer,
    PayoutSerializer,
    PayoutCreateSerializer,
)
from .services import create_payout, save_idempotency_key, get_idempotency_response
from .tasks import process_payout

logger = logging.getLogger(__name__)


def get_merchant(request):
    """
    Helper: returns the merchant for the current request.
    Uses the authenticated user in production.
    Falls back to the first merchant in the DB for development.
    NOTE: In production, remove the AllowAny permission and the fallback.
    """
    if request.user.is_authenticated:
        try:
            return request.user.merchant, None
        except Merchant.DoesNotExist:
            return None, Response(
                {'error': 'No merchant account found.'},
                status=status.HTTP_404_NOT_FOUND
            )
    # Development fallback — not for production
    merchant = Merchant.objects.last()
    if not merchant:
        return None, Response(
            {'error': 'No merchant account found.'},
            status=status.HTTP_404_NOT_FOUND
        )
    return merchant, None


class MerchantDetailView(APIView):

    permission_classes = [AllowAny]

    def get(self, request):
        merchant, err = get_merchant(request)
        if err:
            return err
        return Response(MerchantSerializer(merchant).data)


class LedgerEntryListView(APIView):
    """
    GET /api/v1/ledger/
    """
    permission_classes = [AllowAny]

    def get(self, request):
        merchant, err = get_merchant(request)
        if err:
            return err
        entries = merchant.ledger_entries.select_related('merchant')[:50]
        return Response(LedgerEntrySerializer(entries, many=True).data)


class BankAccountListView(APIView):
    """
    GET /api/v1/bank-accounts/
    """
    permission_classes = [AllowAny]

    def get(self, request):
        merchant, err = get_merchant(request)
        if err:
            return err
        accounts = merchant.bank_accounts.filter(is_active=True)
        return Response(BankAccountSerializer(accounts, many=True).data)


class PayoutListCreateView(APIView):
    """
    GET  /api/v1/payouts/   — list all payouts for the merchant
    POST /api/v1/payouts/   — create a new payout request

    """
    permission_classes = [AllowAny]

    def get(self, request):
        merchant, err = get_merchant(request)
        if err:
            return err
        payouts = merchant.payouts.all()
        return Response(PayoutSerializer(payouts, many=True).data)

    def post(self, request):
        merchant, err = get_merchant(request)
        if err:
            return err

        idempotency_key = request.headers.get('Idempotency-Key', '').strip()

        if idempotency_key:
            cached_body, cached_status = get_idempotency_response(merchant, idempotency_key)
            if cached_body is not None:
                return Response(cached_body, status=cached_status)

        serializer = PayoutCreateSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
        try:
            payout = create_payout(
                merchant_id=merchant.id,
                amount_paise=serializer.validated_data['amount_paise'],
                bank_account_id=serializer.validated_data['bank_account_id'],
                idempotency_key=idempotency_key,
            )
        except ValueError as e:
            return Response({'error': str(e)}, status=status.HTTP_400_BAD_REQUEST)

        response_data = PayoutSerializer(payout).data
        response_status_code = status.HTTP_201_CREATED

        if idempotency_key:
            save_idempotency_key(merchant, idempotency_key, response_data, response_status_code)

        try:
            process_payout.delay(str(payout.id))
        except Exception as e:
            logger.error(f"Failed to queue payout {payout.id} to Celery: {e}")

        return Response(response_data, status=response_status_code)


class PayoutDetailView(APIView):
    """
    GET /api/v1/payouts/<payout_id>/
  
    """
    permission_classes = [AllowAny]

    def get(self, request, payout_id):
        merchant, err = get_merchant(request)
        if err:
            return err
        try:
            payout = merchant.payouts.get(id=payout_id)
        except Payout.DoesNotExist:
            return Response({'error': 'Payout not found.'}, status=status.HTTP_404_NOT_FOUND)
        return Response(PayoutSerializer(payout).data)
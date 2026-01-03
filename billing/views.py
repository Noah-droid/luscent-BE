from rest_framework import generics, permissions, status
from rest_framework.response import Response
from rest_framework.views import APIView
from .models import Transaction
from .serializers import TransactionSerializer

class UsageSummaryView(APIView):
    """
    Returns current token balance and recent transaction history.
    """
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        user = request.user
        transactions = Transaction.objects.filter(user=user).order_by('-created_at')[:50]
        serializer = TransactionSerializer(transactions, many=True)
        
        return Response({
            "token_balance": user.token_balance,
            "recent_transactions": serializer.data
        })

class TransactionListView(generics.ListAPIView):
    """
    Returns full transaction history.
    """
    serializer_class = TransactionSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        return Transaction.objects.filter(user=self.request.user).order_by('-created_at')

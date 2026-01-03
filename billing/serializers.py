from rest_framework import serializers
from .models import Transaction

class TransactionSerializer(serializers.ModelSerializer):
    class Meta:
        model = Transaction
        fields = ['id', 'amount', 'balance_after', 'transaction_type', 'description', 'reference_id', 'created_at']
        read_only_fields = fields

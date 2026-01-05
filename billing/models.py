from django.db import models
from django.conf import settings

class Transaction(models.Model):
    TRANSACTION_TYPES = [
        ('PURCHASE', 'Purchase'),
        ('USAGE', 'Usage'),
        ('BONUS', 'Bonus'),
        ('REFUND', 'Refund'),
    ]

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='transactions')
    amount = models.IntegerField(help_text="Positive for credit, negative for debit")
    balance_after = models.IntegerField()
    transaction_type = models.CharField(max_length=20, choices=TRANSACTION_TYPES)
    description = models.CharField(max_length=255)
    reference_id = models.CharField(max_length=100, blank=True, null=True, help_text="TestRun ID")
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.user.email} - {self.amount} ({self.transaction_type})"

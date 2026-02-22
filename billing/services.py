from .models import Transaction
from django.db import transaction
import logging

logger = logging.getLogger(__name__)

def deduct_tokens(user, amount, description, ref_id=None):
    """
    Deduct tokens from user and log transaction.
    Returns True if successful, False if insufficient founds.
    """
    if amount <= 0:
        raise ValueError("Amount to deduct must be positive")

    with transaction.atomic():
        # Lock user row for update? (Optimistic for now)
        if user.token_balance < amount:
            return False
        
        user.token_balance -= amount
        user.save(update_fields=['token_balance'])
        
        Transaction.objects.create(
            user=user,
            amount=-amount,
            balance_after=user.token_balance,
            transaction_type='USAGE',
            description=description,
            reference_id=str(ref_id) if ref_id else None
        )
        return True

def add_tokens(user, amount, description, ref_id=None, type='PURCHASE'):
    """
    Add tokens to user and log transaction.
    """
    if amount <= 0:
        raise ValueError("Amount to add must be positive")
        
    with transaction.atomic():
        user.token_balance += amount
        user.save(update_fields=['token_balance'])
        
        Transaction.objects.create(
            user=user,
            amount=amount,
            balance_after=user.token_balance,
            transaction_type=type,
            description=description,
            reference_id=str(ref_id) if ref_id else None
        )
        return True

def calculate_test_cost(runner_type):
    """
    Returns the token cost for a specific runner type or session type.
    """
    costs = {
        'http': 1,
        'browser': 5,
        'load': 10,
        'agent_mission_entry': 20,
        'security_audit_entry': 150,
        'ai_generation': 3,
        'shell_command': 2,
        'mail_action': 2,
        'vision_request': 3
    }
    return costs.get(runner_type.lower(), 1)

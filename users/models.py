from django.contrib.auth.models import AbstractUser
from django.db import models
import secrets


class User(AbstractUser):
    """Custom user model"""
    email = models.EmailField(unique=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    # Usage tracking
    test_runs_count = models.IntegerField(default=0)
    test_runs_limit = models.IntegerField(default=100)  # Free tier limit
    
    # Verification
    is_verified = models.BooleanField(default=False)
    verification_token = models.CharField(max_length=100, blank=True, null=True)
    
    USERNAME_FIELD = 'email'
    REQUIRED_FIELDS = ['username']
    
    def __str__(self):
        return self.email
    
    def can_run_test(self):
        """Check if user has remaining test runs"""
        return self.test_runs_count < self.test_runs_limit
    
    def increment_test_runs(self):
        """Increment test run counter"""
        self.test_runs_count += 1
        self.save()


class APIToken(models.Model):
    """API tokens for CLI authentication"""
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='api_tokens')
    token = models.CharField(max_length=64, unique=True, db_index=True)
    name = models.CharField(max_length=100, default="Default Token")
    created_at = models.DateTimeField(auto_now_add=True)
    last_used_at = models.DateTimeField(null=True, blank=True)
    is_active = models.BooleanField(default=True)
    
    class Meta:
        ordering = ['-created_at']
    
    def __str__(self):
        return f"{self.user.email} - {self.name}"
    
    @staticmethod
    def generate_token():
        """Generate a secure random token"""
        return f"qai_{secrets.token_urlsafe(32)}"
    
    def save(self, *args, **kwargs):
        if not self.token:
            self.token = self.generate_token()
        super().save(*args, **kwargs)



from django.contrib.auth.models import AbstractUser
from django.db import models
import secrets
from cryptography.fernet import Fernet
from django.conf import settings
import base64


class User(AbstractUser):
    """Custom user model"""
    email = models.EmailField(unique=True)
    full_name = models.CharField(max_length=255, blank=True, null=True)
    organization_name = models.CharField(max_length=255, blank=True, null=True)
    onboarding_completed = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    # Usage tracking
    test_runs_count = models.IntegerField(default=0)
    token_balance = models.IntegerField(default=100) # Start with 100 free tokens
    
    # Verification
    is_verified = models.BooleanField(default=False)
    verification_token = models.CharField(max_length=100, blank=True, null=True)
    
    # Integrations
    github_token = models.CharField(max_length=255, blank=True, null=True, help_text="User's GitHub Personal Access Token for repo fetching")
    github_username = models.CharField(max_length=255, blank=True, null=True)
    
    USERNAME_FIELD = 'email'
    REQUIRED_FIELDS = ['username']
    
    def __str__(self):
        return self.email
    
    def increment_test_runs(self):
        """Increment the lifetime test run counter"""
        self.test_runs_count += 1
        self.save(update_fields=['test_runs_count'])


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
        return f"luscent{secrets.token_urlsafe(32)}"
    
    def save(self, *args, **kwargs):
        if not self.token:
            self.token = self.generate_token()
        super().save(*args, **kwargs)
class TestCredential(models.Model):
    """Credentials for agents to use when encountering 3rd party auth.

    Credentials are scoped to a single project whenever possible (the model
    the QA agent actually runs against), so test secrets live with the project
    they belong to instead of being copied across the board. A credential with
    ``project=None`` is a shared, org-wide credential managed by staff.
    """
    PROVIDER_CHOICES = [
        ('google', 'Google'),
        ('github', 'GitHub'),
        ('email', 'Standard Email/Password'),
        ('other', 'Other'),
    ]
    
    provider = models.CharField(max_length=20, choices=PROVIDER_CHOICES)
    email = models.EmailField()
    password = models.CharField(max_length=255, help_text="Stored as provided. Production should use encryption.")
    metadata = models.JSONField(default=dict, blank=True, help_text="Extra config like client_id, secret, or specific target domains")
    description = models.TextField(blank=True, null=True, help_text="Context on where and why to use these credentials")
    is_active = models.BooleanField(default=True)
    project = models.ForeignKey(
        'projects.Project',
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name='test_credentials',
        help_text='Project this credential is scoped to. Empty = shared across projects (staff-managed).'
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Agent Credential"
        verbose_name_plural = "Agent Credentials"

    @property
    def scope_label(self):
        return 'shared' if self.project_id is None else 'project'

    def _get_fernet(self):
        # Derive a 32-byte key from SECRET_KEY
        key = base64.urlsafe_b64encode(settings.SECRET_KEY[:32].encode().ljust(32, b'0'))
        return Fernet(key)

    def save(self, *args, **kwargs):
        # Encrypt password only if it looks like plain text (not already encrypted)
        # Simple heuristic: Fernet tokens usually start with 'gAAAA'
        if self.password and not self.password.startswith('gAAAA'):
            f = self._get_fernet()
            self.password = f.encrypt(self.password.encode()).decode()
        super().save(*args, **kwargs)

    @property
    def decrypted_password(self):
        if not self.password:
            return ""
        try:
            f = self._get_fernet()
            return f.decrypt(self.password.encode()).decode()
        except Exception:
            return self.password

    def __str__(self):
        return f"{self.get_provider_display()} - {self.email}"

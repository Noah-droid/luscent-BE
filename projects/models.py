from django.db import models
from django.contrib.auth import get_user_model
from encrypted_fields.fields import EncryptedJSONField
import uuid
User = get_user_model()


class Project(models.Model):

    id = models.UUIDField(primary_key=True, default=uuid.uuid4)

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="projects")
    name = models.CharField(max_length=200)
    description = models.TextField(blank=True, null=True)
    user_story = models.TextField(blank=True, null=True)
    environment_variables = EncryptedJSONField(default=dict, blank=True, help_text="Key-value pairs for test generation")

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    # Advanced: Repo Integration (Whitebox Testing)
    repo_url = models.URLField(blank=True, null=True, help_text="GitHub/GitLab repository URL for white-box testing")
    repo_branch = models.CharField(max_length=100, default='main', blank=True, help_text="Branch to clone")
    repo_type = models.CharField(max_length=50, blank=True, null=True, help_text="e.g., 'frontend', 'backend', 'mobile', 'fullstack'")
    
    # Storage quota tracking
    storage_used_bytes = models.BigIntegerField(default=0, help_text="Total storage used in bytes")
    storage_quota_bytes = models.BigIntegerField(default=104_857_600, help_text="Storage quota in bytes (default: 100 MB)")
    
    def __str__(self):
        return self.name
    
    @property
    def storage_used_mb(self):
        """Get storage used in MB."""
        return self.storage_used_bytes / (1024 * 1024)
    
    @property
    def storage_quota_mb(self):
        """Get storage quota in MB."""
        return self.storage_quota_bytes / (1024 * 1024)
    
    @property
    def storage_percentage_used(self):
        """Get percentage of storage quota used."""
        if self.storage_quota_bytes == 0:
            return 0
        return (self.storage_used_bytes / self.storage_quota_bytes) * 100
    
    def can_upload(self, file_size_bytes):
        """Check if user has enough quota to upload a file."""
        return (self.storage_used_bytes + file_size_bytes) <= self.storage_quota_bytes
    
    def increment_storage(self, bytes_added):
        """Increment storage usage."""
        self.storage_used_bytes += bytes_added
        self.save(update_fields=['storage_used_bytes'])
    
    def decrement_storage(self, bytes_removed):
        """Decrement storage usage (when deleting files)."""
        self.storage_used_bytes = max(0, self.storage_used_bytes - bytes_removed)
        self.save(update_fields=['storage_used_bytes'])







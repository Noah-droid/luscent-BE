from django.db import models
from projects.models import Project
from encrypted_fields.fields import EncryptedCharField, EncryptedJSONField
import uuid

class Collection(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4)
    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="collections")
    name = models.CharField(max_length=200)
    description = models.TextField(blank=True, null=True)
    user_story = models.TextField(blank=True, null=True)
    
    # Optional shared configuration
    base_url = models.CharField(max_length=500, blank=True, null=True)
    headers = EncryptedJSONField(default=dict, blank=True)
    
    SOURCE_CHOICES = [
        ("manual", "Manual"),
        ("swagger", "Swagger"),
        ("postman", "Postman"),
        ("crawler", "Crawler"),
        ("browser", "Browser"),
    ]
    source = models.CharField(max_length=20, choices=SOURCE_CHOICES, default="manual")
    
    # Scheduling fields for "period checks"
    is_scheduled = models.BooleanField(default=False)
    schedule_interval = models.IntegerField(
        choices=[(5, "5 Minutes"), (15, "15 Minutes"), (60, "1 Hour"), (1440, "24 Hours")],
        default=60,
        null=True, blank=True
    )
    last_scheduled_run_at = models.DateTimeField(null=True, blank=True)
    
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name_plural = "Collections"
        unique_together = ("project", "name")

    def __str__(self):
        return f"{self.project.name} / {self.name}"


class Endpoint(models.Model):
    METHOD_CHOICES = [
        ("GET", "GET"),
        ("POST", "POST"),
        ("PUT", "PUT"),
        ("PATCH", "PATCH"),
        ("DELETE", "DELETE"),
    ]

    collection = models.ForeignKey(Collection, on_delete=models.CASCADE, related_name="endpoints")
    
    name = models.CharField(max_length=200)
    method = models.CharField(max_length=10, choices=METHOD_CHOICES)
    url = models.CharField(max_length=500) # Full URL or path

    # Auth for this specific endpoint
    AUTH_CHOICES = [
        ("none", "No Auth"),
        ("api_key", "API Key"),
        ("bearer", "Bearer Token"),
        ("basic", "Basic Auth"),
    ]
    auth_type = models.CharField(max_length=20, choices=AUTH_CHOICES, default="none")
    auth_value = EncryptedCharField(max_length=500, blank=True, null=True) 

    query_params = EncryptedJSONField(default=dict, blank=True)
    headers = EncryptedJSONField(default=dict, blank=True)
    request_body = EncryptedJSONField(default=dict, blank=True)
    description = models.TextField(blank=True, null=True)
    
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ("collection", "method", "url")

    def __str__(self):
        return f"{self.method} {self.url}"

    def clean(self):
        """Validate URL security before saving."""
        from django.core.exceptions import ValidationError
        from test_cases.security import validate_url_security
        from django.conf import settings
        
        # Validate URL security
        allow_localhost = getattr(settings, 'DEBUG', False)
        is_valid, error_message = validate_url_security(self.url, allow_localhost=allow_localhost)
        
        if not is_valid:
            raise ValidationError({
                'url': f'Security validation failed: {error_message}'
            })

    def save(self, *args, **kwargs):
        """Override save to ensure validation runs."""
        self.full_clean()
        super().save(*args, **kwargs)


class ImportJob(models.Model):
    """
    A queued/background import of endpoints into a collection (Swagger/OpenAPI, crawler).
    Imports run through Celery when available; the row doubles as the source of truth
    for status so the UI can show progress without touching the broker.
    """

    KIND_CHOICES = [("swagger", "Swagger"), ("crawler", "Crawler")]
    SOURCE_CHOICES = [("url", "URL"), ("file", "File")]
    STATUS_CHOICES = [
        ("queued", "Queued"),
        ("running", "Running"),
        ("success", "Success"),
        ("failed", "Failed"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    collection = models.ForeignKey(Collection, on_delete=models.CASCADE, related_name="import_jobs")
    kind = models.CharField(max_length=20, choices=KIND_CHOICES, default="swagger")
    source = models.CharField(max_length=20, choices=SOURCE_CHOICES, default="url")
    # Human-readable label: URL for url imports, filename for file imports
    spec_name = models.CharField(max_length=500, blank=True, default="")
    # Inline spec payload for file imports (worker may run on a different container).
    # Cleared once the job finishes to keep rows lean.
    spec_text = models.TextField(blank=True, null=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="queued")
    skip_validation = models.BooleanField(default=False)
    imported_count = models.PositiveIntegerField(default=0)
    error = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.kind}/{self.source} {self.collection_id} -> {self.status}"

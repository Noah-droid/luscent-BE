from django.db import models
from projects.models import Project
from encrypted_fields.fields import EncryptedCharField, EncryptedJSONField


class Collection(models.Model):
    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="collections")
    name = models.CharField(max_length=200)
    description = models.TextField(blank=True, null=True)
    
    # Optional shared configuration
    base_url = models.CharField(max_length=500, blank=True, null=True)
    headers = EncryptedJSONField(default=dict, blank=True)
    
    SOURCE_CHOICES = [
        ("manual", "Manual"),
        ("swagger", "Swagger"),
        ("postman", "Postman"),
        ("crawler", "Crawler"),
    ]
    source = models.CharField(max_length=20, choices=SOURCE_CHOICES, default="manual")
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

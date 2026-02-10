from django.db import models
from collection.models import Endpoint

class TestCase(models.Model):
    PRIORITY_CHOICES = [
        ("critical", "Critical"),
        ("high", "High"),
        ("medium", "Medium"),
        ("low", "Low"),
    ]

    endpoint = models.ForeignKey(Endpoint, on_delete=models.CASCADE, related_name="test_cases")
    name = models.CharField(max_length=200, blank=True, null=True)
    description = models.TextField(blank=True, null=True)
    
    # Execution data
    headers = models.JSONField(default=dict, blank=True)
    query_params = models.JSONField(default=dict, blank=True)
    body = models.JSONField(default=dict, blank=True)
    
    # Assertions
    expected_status = models.IntegerField(default=200)
    assertions = models.JSONField(default=list, blank=True) 
    # Example format: [{"type": "json_path", "field": "$.id", "operator": "exists", "value": None}]

    # Advanced Runner Fields
    RUNNER_CHOICES = [
        ("http", "HTTP Request"),
        ("browser", "Browser (Playwright)"),
        ("load", "Load Test (Locust)"),
    ]
    runner_type = models.CharField(max_length=20, choices=RUNNER_CHOICES, default="http")
    test_script = models.TextField(blank=True, null=True, help_text="AI-generated python/js script for execution")
    
    # Toggle for Visual AI Check
    use_visual_ai = models.BooleanField(default=False, help_text="If true, takes screenshot and analyzes with AI")

    # Classification
    CATEGORY_CHOICES = [
        ("functional", "Functional"),
        ("smoke", "Smoke"),
        ("regression", "Regression"),
        ("security", "Security"),
        ("performance", "Performance"),
        ("e2e", "End-to-End"),
    ]
    category = models.CharField(max_length=20, choices=CATEGORY_CHOICES, default="functional")

    LAYER_CHOICES = [
        ("backend", "Backend (API)"),
        ("frontend", "Frontend (UI)"),
    ]
    layer = models.CharField(max_length=20, choices=LAYER_CHOICES, default="backend")

    priority = models.CharField(max_length=10, choices=PRIORITY_CHOICES, default="medium")
    tags = models.JSONField(default=list, blank=True, help_text="e.g. ['SCENARIO:HAPPY_PATH', 'CRITICAL']")
    user_story = models.TextField(blank=True, null=True, help_text="User story/requirements context used to generate this test")
    ai_generated = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.name} ({self.endpoint})"
    
    def clean(self):
        """Validate test case data before saving."""
        from django.core.exceptions import ValidationError
        import json
        
        # Validate name length
        if self.name and len(self.name) > 200:
            raise ValidationError({'name': 'Test name too long (max 200 characters)'})
        
        # Validate description length
        if self.description and len(self.description) > 5000:
            raise ValidationError({'description': 'Description too long (max 5000 characters)'})
        
        # Validate test_script size
        if self.test_script and len(self.test_script) > 50000:  # 50 KB
            raise ValidationError({'test_script': 'Test script too large (max 50 KB)'})
        
        # Validate JSON field sizes
        try:
            if self.headers:
                headers_str = json.dumps(self.headers)
                if len(headers_str) > 10000:  # 10 KB
                    raise ValidationError({'headers': 'Headers too large (max 10 KB)'})
            
            if self.query_params:
                params_str = json.dumps(self.query_params)
                if len(params_str) > 10000:
                    raise ValidationError({'query_params': 'Query params too large (max 10 KB)'})
            
            if self.body:
                body_str = json.dumps(self.body)
                if len(body_str) > 100000:  # 100 KB
                    raise ValidationError({'body': 'Request body too large (max 100 KB)'})
            
            if self.assertions:
                assertions_str = json.dumps(self.assertions)
                if len(assertions_str) > 10000:
                    raise ValidationError({'assertions': 'Assertions too large (max 10 KB)'})
                    
        except (TypeError, ValueError) as e:
            raise ValidationError(f'Invalid JSON data: {str(e)}')
    
    def save(self, *args, **kwargs):
        """Override save to ensure validation runs."""
        self.full_clean()
        super().save(*args, **kwargs)


class TestRun(models.Model):
    STATUS_CHOICES = [
        ("pending", "Pending"),
        ("running", "Running"),
        ("passed", "Passed"),
        ("failed", "Failed"),
        ("error", "Error"),
    ]

    test_case = models.ForeignKey(TestCase, on_delete=models.CASCADE, related_name="runs")
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="pending")
    
    # Results
    response_status = models.IntegerField(null=True, blank=True)
    response_body = models.JSONField(null=True, blank=True)
    response_headers = models.JSONField(null=True, blank=True)
    response_time_ms = models.IntegerField(default=0)
    
    error_message = models.TextField(blank=True, null=True)
    logs = models.TextField(blank=True, null=True)
    
    # Artifacts (stored in Cloudinary)
    screenshot_url = models.URLField(max_length=500, null=True, blank=True, help_text="Cloudinary URL")
    screenshot_public_id = models.CharField(max_length=255, null=True, blank=True, help_text="Cloudinary public ID for deletion")
    screenshot_size_bytes = models.IntegerField(default=0, help_text="File size for quota tracking")
    
    executed_at = models.DateTimeField(auto_now_add=True)
    batch_id = models.UUIDField(null=True, blank=True, db_index=True, help_text="Group ID for batch executions")
    triggered_by = models.CharField(max_length=50, choices=[("manual", "Manual"), ("ai", "AI Auto-Pilot"), ("webhook", "Webhook"), ("scheduled", "Scheduled")], default="manual")
    
    # New: Hybrid/Self-Hosted Support
    # assigned_runner = models.ForeignKey(
    #     'remote_runners.RemoteRunner', 
    #     on_delete=models.SET_NULL, 
    #     null=True, 
    #     blank=True, 
    #     related_name="assigned_runs",
    #     help_text="The remote runner assigned to execute this test"
    # )

    def __str__(self):
        return f"Run {self.id} for {self.test_case.name} - {self.status}"



from django.db import models
from django.contrib.auth import get_user_model

User = get_user_model()


class Project(models.Model):

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="projects")
    name = models.CharField(max_length=200)
    description = models.TextField(blank=True, null=True)
    environment_variables = models.JSONField(default=dict, blank=True, help_text="Key-value pairs for test generation (e.g. {'VALID_USER': 'admin'})")

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return self.name







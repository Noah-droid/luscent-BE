from rest_framework import serializers
from .models import Project


class ProjectSerializer(serializers.ModelSerializer):
    test_runs_count = serializers.SerializerMethodField()

    class Meta:
        model = Project
        fields = [
            "id",
            "name",
            "description",
            "user_story",
            "environment_variables",
            "test_runs_count",
            "storage_used_bytes",
            "storage_quota_bytes",
            "created_at",
            "updated_at",
        ]

        extra_kwargs = {
            'environment_variables': {'required': False},
        }
        read_only_fields = ["id", "created_at", "updated_at"]

    def get_test_runs_count(self, obj):
        from test_cases.models import TestRun
        return TestRun.objects.filter(test_case__endpoint__collection__project=obj).count()

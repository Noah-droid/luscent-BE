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
            "repo_url",
            "repo_branch",
            "repo_type",
            "target_url",
            "test_runs_count",
            "storage_used_bytes",
            "storage_quota_bytes",
            "created_at",
            "updated_at",
        ]

        extra_kwargs = {
            'environment_variables': {'required': False},
            'target_url': {'write_only': True, 'required': False},
        }
        read_only_fields = ["id", "created_at", "updated_at"]

    def create(self, validated_data):
        target_url = validated_data.pop('target_url', None)
        project = super().create(validated_data)
        
        # If target_url is provided, create a default collection
        if target_url:
            from collection.models import Collection
            Collection.objects.create(
                project=project,
                name="Default",
                base_url=target_url,
                description=f"Auto-generated collection for {project.name}"
            )
        return project

    def get_test_runs_count(self, obj):
        from test_cases.models import TestRun
        return TestRun.objects.filter(test_case__endpoint__collection__project=obj).count()

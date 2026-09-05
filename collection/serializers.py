from rest_framework import serializers
from .models import Collection, Endpoint, ImportJob


class EndpointSerializer(serializers.ModelSerializer):
    class Meta:
        model = Endpoint
        fields = [
            "id",
            "collection",
            "name",
            "method",
            "url",
            "auth_type",
            "auth_value",
            "query_params",
            "headers",
            "request_body",
            "description",
            "created_at",
        ]
        read_only_fields = ["id", "created_at", "collection"]

    def validate_url(self, value):
        # Accept full URLs or relative paths (e.g. "/api/endpoint")
        if not (value.startswith(("http://", "https://", "ws://", "wss://", "/"))):
            raise serializers.ValidationError("URL must start with http://, https://, ws://, wss://, or / (relative path)")
        return value


class CollectionSerializer(serializers.ModelSerializer):
    endpoints_count = serializers.IntegerField(source="endpoints.count", read_only=True)

    class Meta:
        model = Collection
        fields = [
            "id",
            "project",
            "name",
            "description",
            "user_story",
            "base_url",
            "headers",
            "source",
            "endpoints_count",
            "is_scheduled",
            "schedule_interval",
            "last_scheduled_run_at",
            "created_at",
        ]
        read_only_fields = ["id", "project", "created_at", "endpoints_count", "last_scheduled_run_at"]


class ImportJobSerializer(serializers.ModelSerializer):
    """
    Import jobs are always nested under a collection/project, so include those
    relations inline — the UI needs them to navigate/toast without extra calls.
    spec_text is deliberately excluded (payload only, cleared after processing).
    """

    collection_id = serializers.UUIDField(source="collection.id", read_only=True)
    collection_name = serializers.CharField(source="collection.name", read_only=True)
    project_id = serializers.UUIDField(source="collection.project.id", read_only=True)
    project_name = serializers.CharField(source="collection.project.name", read_only=True)

    class Meta:
        model = ImportJob
        fields = [
            "id",
            "kind",
            "source",
            "spec_name",
            "status",
            "skip_validation",
            "imported_count",
            "error",
            "created_at",
            "started_at",
            "finished_at",
            "collection_id",
            "collection_name",
            "project_id",
            "project_name",
        ]
        read_only_fields = fields

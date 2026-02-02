from rest_framework import serializers
from .models import Collection, Endpoint


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
        read_only_fields = ["id", "created_at"]

    def validate_url(self, value):
        if not value.startswith(("http://", "https://", "ws://", "wss://")):
            raise serializers.ValidationError("URL must start with http://, https://, ws:// or wss://")
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
        read_only_fields = ["id", "project", "created_at", "source", "endpoints_count", "last_scheduled_run_at"]

from rest_framework import serializers
from .models import Collection


class CollectionSerializer(serializers.ModelSerializer):
    source = serializers.CharField(read_only=True)

    class Meta:
        model = Collection
        fields = [
            "id",
            "project",
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
            "source"
        ]
        read_only_fields = ["id", "project", "created_at", "source"]

    def validate_url(self, value):
        if not value.startswith(("http://", "https://", "ws://", "wss://")):
            raise serializers.ValidationError("URL must start with http://, https://, ws:// or wss://")
        return value




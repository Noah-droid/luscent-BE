from rest_framework import serializers
from .models import TestCase, TestRun

class TestCaseSerializer(serializers.ModelSerializer):
    class Meta:
        model = TestCase
        fields = "__all__"

class TestRunSerializer(serializers.ModelSerializer):
    test_case_name = serializers.ReadOnlyField(source='test_case.name')
    endpoint_name = serializers.ReadOnlyField(source='test_case.endpoint.name')
    endpoint_method = serializers.ReadOnlyField(source='test_case.endpoint.method')
    endpoint_url = serializers.ReadOnlyField(source='test_case.endpoint.url')

    class Meta:
        model = TestRun
        fields = "__all__"

from rest_framework import serializers
from .models import TestCase, TestRun

class TestCaseSerializer(serializers.ModelSerializer):
    class Meta:
        model = TestCase
        fields = "__all__"

class TestRunSerializer(serializers.ModelSerializer):
    class Meta:
        model = TestRun
        fields = "__all__"

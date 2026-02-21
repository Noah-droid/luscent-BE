from rest_framework import serializers
from .models import TestCase, TestRun, AgentMission, AgentMissionStep, AgentPrompt

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

class AgentMissionStepSerializer(serializers.ModelSerializer):
    class Meta:
        model = AgentMissionStep
        fields = "__all__"

class AgentPromptSerializer(serializers.ModelSerializer):
    class Meta:
        model = AgentPrompt
        fields = "__all__"

class AgentMissionSerializer(serializers.ModelSerializer):
    steps = AgentMissionStepSerializer(many=True, read_only=True)
    prompts = AgentPromptSerializer(many=True, read_only=True)
    collection_name = serializers.ReadOnlyField(source='collection.name')

    class Meta:
        model = AgentMission
        fields = "__all__"

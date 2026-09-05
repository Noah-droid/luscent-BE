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

class SessionReportSerializer(serializers.ModelSerializer):
    """
    A rich, structured serializer for session reports.
    Designed for both human UI and programmatic (agent) consumption.
    """
    steps = AgentMissionStepSerializer(many=True, read_only=True)
    collection_name = serializers.ReadOnlyField(source='collection.name')
    project_name = serializers.ReadOnlyField(source='collection.project.name')
    collection_id = serializers.PrimaryKeyRelatedField(source='collection', read_only=True)
    project_id = serializers.PrimaryKeyRelatedField(source='collection.project', read_only=True)
    steps_count = serializers.SerializerMethodField()
    pass_rate = serializers.FloatField(read_only=True)
    regressions = serializers.ListField(read_only=True)
    previous_session_id = serializers.PrimaryKeyRelatedField(
        source='previous_session', read_only=True, allow_null=True
    )
    
    class Meta:
        model = AgentMission
        fields = [
            'id', 'batch_id', 'status', 'mission_type', 'user_story',
            'collection_name', 'project_name', 'collection_id', 'project_id',
            'total_steps', 'passed_steps', 'failed_steps',
            'pass_rate', 'duration_seconds', 'completed_at',
            'summary', 'error_message',
            'previous_session_id', 'regressions',
            'steps', 'steps_count',
            'session_url', 'app_url',
            'created_at', 'updated_at',
        ]

    def get_steps_count(self, obj):
        return obj.steps.count()


class SessionHistorySerializer(serializers.ModelSerializer):
    """
    Lightweight serializer for listing past sessions.
    Omits full steps list for performance.
    """
    collection_name = serializers.ReadOnlyField(source='collection.name')
    project_name = serializers.ReadOnlyField(source='collection.project.name')
    steps_count = serializers.SerializerMethodField()
    pass_rate = serializers.FloatField(read_only=True)

    class Meta:
        model = AgentMission
        fields = [
            'id', 'batch_id', 'status', 'mission_type', 'user_story',
            'collection_name', 'project_name',
            'total_steps', 'passed_steps', 'failed_steps',
            'pass_rate', 'duration_seconds', 'completed_at',
            'summary', 'error_message',
            'created_at',
        ]

    def get_steps_count(self, obj):
        return obj.total_steps or obj.steps.count()


class AgentMissionSerializer(serializers.ModelSerializer):
    steps = AgentMissionStepSerializer(many=True, read_only=True)
    prompts = AgentPromptSerializer(many=True, read_only=True)
    collection_name = serializers.ReadOnlyField(source='collection.name')
    project_name = serializers.ReadOnlyField(source='collection.project.name')
    steps_count = serializers.SerializerMethodField()
    pass_rate = serializers.FloatField(read_only=True)

    class Meta:
        model = AgentMission
        fields = "__all__"

    def get_steps_count(self, obj):
        return obj.total_steps or obj.steps.count()

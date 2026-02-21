from django.urls import path
from .views import (
    # TestCaseListCreateView, 
    TestCaseDetailView, RunTestView,
    TestRunListView, TestRunDetailView, DraftTestPlanView, BatchCreateTestsView,
    TestConfigView, RefineTestDraftView,
    TriggerTestRunView, ProjectStatusView, ProjectAutoPilotView, ProjectSecurityAuditView,
    CollectionAutoPilotView, CollectionStatusView,
    AgentMissionListView, AgentMissionDetailView, AgentMissionPromptView
)

urlpatterns = [
    path("ai/scenarios/", TestConfigView.as_view(), name="ai-config"),
    path("webhook/trigger/", TriggerTestRunView.as_view(), name="webhook-trigger"),
    # path("", TestCaseListCreateView.as_view(), name="testcase-list-create"),
    path("ai/draft/", DraftTestPlanView.as_view(), name="ai-draft-plan"),
    path("ai/refine/", RefineTestDraftView.as_view(), name="ai-refine-draft"),
    path("batch-create/", BatchCreateTestsView.as_view(), name="testcase-batch-create"),
    path("<int:pk>/", TestCaseDetailView.as_view(), name="testcase-detail"),
    path("<int:pk>/run/", RunTestView.as_view(), name="testcase-run"),
    path("<int:test_case_id>/runs/", TestRunListView.as_view(), name="testcase-runs"),
    path("runs/", TestRunListView.as_view(), name="testrun-list"),
    path("runs/<int:pk>/", TestRunDetailView.as_view(), name="testrun-detail"),
    path("projects/<uuid:project_id>/status/", ProjectStatusView.as_view(), name="project-status"),
    path("projects/<uuid:project_id>/auto-pilot/", ProjectAutoPilotView.as_view(), name="project-auto-pilot"),
    path("projects/<uuid:project_id>/security-audit/", ProjectSecurityAuditView.as_view(), name="project-security-audit"),
    path("collections/<uuid:collection_id>/status/", CollectionStatusView.as_view(), name="collection-status"),
    path("collections/<uuid:collection_id>/auto-pilot/", CollectionAutoPilotView.as_view(), name="collection-auto-pilot"),
    
    # Agent Live Interaction
    path("missions/", AgentMissionListView.as_view(), name="agent-mission-list"),
    path("missions/<uuid:batch_id>/", AgentMissionDetailView.as_view(), name="agent-mission-detail"),
    path("missions/<uuid:batch_id>/prompt/", AgentMissionPromptView.as_view(), name="agent-mission-prompt"),
]


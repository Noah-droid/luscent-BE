from django.urls import path
from .views import (
    # TestCaseListCreateView, 
    TestCaseDetailView, RunTestView,
    TestRunListView, TestRunDetailView, DraftTestPlanView, BatchCreateTestsView,
    TestConfigView, RefineTestDraftView,
    TriggerTestRunView, ProjectStatusView, ProjectRunReportsView, ProjectAutoPilotView, ProjectSecurityAuditView,
    CollectionAutoPilotView, CollectionStatusView,
    AgentMissionListView, AgentMissionDetailView, AgentMissionPromptView,
    SessionReportView, SessionHistoryView, SessionComparisonView,
    DashboardSummaryView, BatchReportView,
    AgentTakeoverView, DatasetExportView
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
    path("projects/<uuid:project_id>/reports/runs/", ProjectRunReportsView.as_view(), name="project-run-reports"),
    path("projects/<uuid:project_id>/auto-pilot/", ProjectAutoPilotView.as_view(), name="project-auto-pilot"),
    path("projects/<uuid:project_id>/security-audit/", ProjectSecurityAuditView.as_view(), name="project-security-audit"),
    path("collections/<uuid:collection_id>/status/", CollectionStatusView.as_view(), name="collection-status"),
    path("collections/<uuid:collection_id>/auto-pilot/", CollectionAutoPilotView.as_view(), name="collection-auto-pilot"),
    
    # Agent Live Interaction
    path("missions/", AgentMissionListView.as_view(), name="agent-mission-list"),
    path("missions/<uuid:batch_id>/", AgentMissionDetailView.as_view(), name="agent-mission-detail"),
    path("missions/<uuid:batch_id>/prompt/", AgentMissionPromptView.as_view(), name="agent-mission-prompt"),
    
    # Session Reports
    path("sessions/<uuid:batch_id>/report/", SessionReportView.as_view(), name="session-report"),
    path("sessions/compare/", SessionComparisonView.as_view(), name="session-comparison"),
    path("collections/<uuid:collection_id>/sessions/", SessionHistoryView.as_view(), name="session-history"),
    
    # Dashboard & Batch
    path("dashboard/", DashboardSummaryView.as_view(), name="dashboard-summary"),
    path("batches/<uuid:batch_id>/report/", BatchReportView.as_view(), name="batch-report"),
    
    # Human Takeover
    path("missions/<uuid:batch_id>/takeover/", AgentTakeoverView.as_view(), name="agent-takeover"),
    
    # Dataset Export
    path("datasets/export/", DatasetExportView.as_view(), name="dataset-export"),
]


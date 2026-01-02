from django.urls import path
from .views import (
    TestCaseListCreateView, TestCaseDetailView, RunTestView,
    TestRunListView, TestRunDetailView, DraftTestPlanView, BatchCreateTestsView,
    TestConfigView,
    TriggerTestRunView
)

urlpatterns = [
    path("ai/scenarios/", TestConfigView.as_view(), name="ai-config"),
    path("webhook/trigger/", TriggerTestRunView.as_view(), name="webhook-trigger"),
    path("", TestCaseListCreateView.as_view(), name="testcase-list-create"),
    path("ai/draft/", DraftTestPlanView.as_view(), name="ai-draft-plan"),
    path("batch-create/", BatchCreateTestsView.as_view(), name="testcase-batch-create"),
    path("<int:pk>/", TestCaseDetailView.as_view(), name="testcase-detail"),
    path("<int:pk>/run/", RunTestView.as_view(), name="testcase-run"),
    path("<int:test_case_id>/runs/", TestRunListView.as_view(), name="testcase-runs"),
    path("runs/<int:pk>/", TestRunDetailView.as_view(), name="testrun-detail"),
]


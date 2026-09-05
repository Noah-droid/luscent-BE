from rest_framework import generics, permissions, status
from rest_framework.response import Response
from rest_framework.views import APIView
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.utils.decorators import method_decorator
from django.views.decorators.cache import cache_page
from django_ratelimit.decorators import ratelimit
from collection.models import Endpoint
from projects.models import Project
from .models import TestCase, TestRun, AgentMission, AgentMissionStep, AgentPrompt
from .serializers import (
    TestCaseSerializer, TestRunSerializer, 
    AgentMissionSerializer, AgentMissionStepSerializer, AgentPromptSerializer,
    SessionReportSerializer, SessionHistorySerializer
)
from .ai_generator import AITestGenerator
from .runner_service import RunnerService
from .reporting import analyze_runs, failure_breakdown, qa_verdict, classify_failure, flaky_summary
from drf_yasg.utils import swagger_auto_schema
from drf_yasg import openapi
try:
    from .tasks import run_test_case_task
    HAS_CELERY = True
except:
    HAS_CELERY = False
import logging

logger = logging.getLogger(__name__)


class TestConfigView(APIView):
    """
    Returns the available configuration options for AI generation.
    Frontend uses this to render the dynamic checkboxes.
    """
    permission_classes = [permissions.AllowAny] # Or IsAuthenticated

    def get(self, request):
        config = {
            "scenarios": [
                {"id": "HAPPY_PATH", "label": "Happy Path", "description": "Standard positive test case with valid data."},
                {"id": "VALIDATION_ERROR", "label": "Validation Errors", "description": "Tests for missing fields, wrong types, and constraint violations."},
                {"id": "AUTH_ERROR", "label": "Auth Failure", "description": "Tests for invalid/missing tokens or wrong credentials."},
                {"id": "EDGE_CASE", "label": "Edge Cases", "description": "Boundary values, empty strings, and logic extremes."},
                {"id": "SECURITY", "label": "Security", "description": "Basic injection attempts (SQLi, XSS, Prompt Injection)."},
            ],
            "runners": [
                {"id": "http", "label": "HTTP Request"},
                {"id": "load", "label": "Load Test"},
                {"id": "browser", "label": "Browser"},
            ]
        }
        return Response(config)


# class TestCaseListCreateView(generics.ListCreateAPIView):
#     # Kept for backward compatibility and manual creation
#     serializer_class = TestCaseSerializer
#     permission_classes = [permissions.IsAuthenticated]

#     def get_queryset(self):
#         return TestCase.objects.filter(collection__project__user=self.request.user)

#     def perform_create(self, serializer):
#         # Ensure we don't allow creating specific IDs if not needed
#         serializer.save()



class DraftTestPlanView(APIView):
    """
    Step 1 of Hybrid Flow: Generate a JSON Draft Plan via AI.
    Does NOT save to DB.
    Input: { "collection_id": 1, "scenarios": ["HAPPY_PATH", "VALIDATION"], ... }
    Output: JSON List of proposed tests.
    """
    permission_classes = [permissions.IsAuthenticated]

    @swagger_auto_schema(
        operation_description="Generate a draft test plan using AI",
        request_body=openapi.Schema(
            type=openapi.TYPE_OBJECT,
            required=['collection'],
            properties={
                'collection': openapi.Schema(type=openapi.TYPE_INTEGER),
                'runner_type': openapi.Schema(type=openapi.TYPE_STRING, enum=['http', 'load', 'browser']),
                'category': openapi.Schema(type=openapi.TYPE_STRING, enum=['functional', 'performance', 'security', "smoke", "regression", 'e2e']),
                'layer': openapi.Schema(type=openapi.TYPE_STRING, enum=['backend', 'frontend']),
                'use_visual_ai': openapi.Schema(type=openapi.TYPE_BOOLEAN, default=False, description="Enable Visual AI analysis for generated tests"),
                'user_story': openapi.Schema(type=openapi.TYPE_STRING, description="Optional: User Story or Requirements to guide test generation"),
                'scenarios': openapi.Schema(type=openapi.TYPE_ARRAY, items=openapi.Items(type=openapi.TYPE_STRING),  
                    description="[HAPPY_PATH, VALIDATION_ERROR, AUTH_ERROR, EDGE_CASE, SECURITY]"),
            }
        ),
        responses={
            200: openapi.Response("Draft generated", openapi.Schema(type=openapi.TYPE_ARRAY, items=openapi.Items(type=openapi.TYPE_OBJECT))),
            400: "Invalid Input"
        }
    )
    @method_decorator(ratelimit(key='user', rate='2/h', method='POST'))
    def post(self, request):
        endpoint_id = request.data.get("endpoint") or request.data.get("collection") # Support legacy key
        if not endpoint_id:
            return Response({"error": "endpoint_id required"}, status=400)

        endpoint_item = get_object_or_404(Endpoint, id=endpoint_id, collection__project__user=request.user)
        
        runner_type = request.data.get("runner_type", "http")
        category = request.data.get("category", "functional")
        layer = request.data.get("layer", "backend")
        use_visual_ai = request.data.get("use_visual_ai", False)
        scenarios = request.data.get("scenarios", []) # e.g. ["HAPPY_PATH"]
        user_story = request.data.get("user_story", None) 

        # Enforce logic: Performance = Load Runner
        if category.lower() == "performance":
            runner_type = "load"
            
        # Billing: Deduct 5 Tokens for AI Generation
        from billing.services import deduct_tokens
        COST = 5
        if not deduct_tokens(request.user, COST, f"AI Generation: {endpoint_item.name}"):
             balance = request.user.token_balance
             if balance <= 0:
                 error_msg = "You have no tokens available. Please top up your balance."
             else:
                 error_msg = f"Insufficient tokens. Required: {COST}, Balance: {balance}."
             return Response({"error": error_msg}, status=402)

        # 1. Fetch Swagger/OpenAPI spec context if available
        spec_context = ""
        # We can look up the collection's shared base_url or endpoints
        project_desc = endpoint_item.collection.project.description or ""
        
        generator = AITestGenerator()
        try:
            # Call AI
            draft_tests = generator.generate_draft_plan(
                endpoint_item, 
                scenarios=scenarios,
                runner_type=runner_type,
                category=category,
                layer=layer,
                project_description=project_desc,
                user_story=(
                    user_story or 
                    endpoint_item.collection.user_story or 
                    endpoint_item.collection.project.user_story or 
                    endpoint_item.collection.project.description
                )
            )
            return Response(draft_tests, status=status.HTTP_200_OK)
        except Exception as e:
            return Response({"error": f"Draft generation failed: {str(e)}"}, status=500)



class RefineTestDraftView(APIView):
    """
    Step 1.5: Refine a specific draft test based on user feedback.
    Input: { "draft": {...}, "instruction": "Make it check for 403 instead" }
    Output: { ...updated_draft... }
    """
    permission_classes = [permissions.IsAuthenticated]
    
    @swagger_auto_schema(
        operation_description="Refine a generated test draft using AI",
        request_body=openapi.Schema(
            type=openapi.TYPE_OBJECT,
            required=['draft', 'instruction'],
            properties={
                'draft': openapi.Schema(type=openapi.TYPE_OBJECT, description="The JSON object of the test case"),
                'instruction': openapi.Schema(type=openapi.TYPE_STRING, description="Instructions for modification"),
                'collection_id': openapi.Schema(type=openapi.TYPE_INTEGER, description="Context for the endpoint")
            }
        ),
        responses={200: "Updated Draft JSON"}
    )
    def post(self, request):
        draft = request.data.get("draft")
        instruction = request.data.get("instruction")
        collection_id = request.data.get("collection_id")
        
        if not draft or not instruction:
            return Response({"error": "Draft and instruction required"}, status=400)
            
        collection_item = None
        if collection_id:
             try:
                 collection_item = Collection.objects.get(id=collection_id, project__user=request.user)
             except Collection.DoesNotExist:
                 return Response({"error": "Collection not found"}, status=404)

        generator = AITestGenerator()
        try:
            updated_draft = generator.refine_test(draft, instruction, collection_item=collection_item)
            return Response(updated_draft, status=200)
        except Exception as e:
            return Response({"error": str(e)}, status=500)


class BatchCreateTestsView(APIView):
    """
    Step 2 of Hybrid Flow: Save approved/edited drafts to DB.
    Input: [ { "name": "Test 1", "collection_id": 1, ... }, ... ]
    Output: [ { "id": 55, ... } ]
    """
    permission_classes = [permissions.IsAuthenticated]

    @swagger_auto_schema(
        operation_description="Batch save generated tests to database",
        request_body=openapi.Schema(
            type=openapi.TYPE_OBJECT,
            properties={
                'collection_id': openapi.Schema(type=openapi.TYPE_INTEGER, description="Global collection ID override"),
                'auto_run': openapi.Schema(type=openapi.TYPE_BOOLEAN, default=True),
                'tests': openapi.Schema(type=openapi.TYPE_ARRAY, items=openapi.Items(type=openapi.TYPE_OBJECT, 
                    properties={
                        'name': openapi.Schema(type=openapi.TYPE_STRING),
                        'collection_id': openapi.Schema(type=openapi.TYPE_INTEGER),
                        'test_script': openapi.Schema(type=openapi.TYPE_STRING),
                        'runner_type': openapi.Schema(type=openapi.TYPE_STRING),
                        'user_story': openapi.Schema(type=openapi.TYPE_STRING),
                       
                    }
                ))
            }
        ),
        responses={
            201: "Tests Created"
        }
    )
    def post(self, request):
        # Expecting structure: { "endpoint_id": 1, "auto_run": true, "tests": [...] }
        payload = request.data
        
        # 1. Parse Root Params
        endpoint_id = payload.get("endpoint_id") or payload.get("endpoint") or payload.get("collection_id") or payload.get("collection")
        should_auto_run = payload.get("auto_run", True) # Default to True
        test_data_list = payload.get("tests", [])

        # Backward compatibility: if they sent a raw list
        if isinstance(payload, list):
            test_data_list = payload
            should_auto_run = True # Default for raw list
            collection_id = None # Must be in items

        if not test_data_list:
             return Response({"error": "No tests provided."}, status=400)

        created_tests = []
        errors = []
        
        # Resolve Endpoint Context globally if provided
        global_endpoint = None
        if endpoint_id:
            try:
                global_endpoint = get_object_or_404(Endpoint, id=endpoint_id, collection__project__user=request.user)
            except:
                pass 
        
        # Save user_story if present in test data
        for index, data in enumerate(test_data_list):
            try:
                # 2. Resolve Endpoint (Item level overrides global)
                endpoint = global_endpoint
                e_id = data.get("endpoint") or data.get("endpoint_id") or data.get("collection") or data.get("collection_id")
                
                if e_id:
                     # If item specifies different endpoint, look it up
                     try:
                         endpoint = Endpoint.objects.get(id=e_id, collection__project__user=request.user)
                     except Endpoint.DoesNotExist:
                         errors.append({"index": index, "error": f"Endpoint {e_id} not found"})
                         continue
                
                if not endpoint:
                     errors.append({"index": index, "error": "Missing endpoint_id (global or item level)"})
                     continue


                test_case = TestCase.objects.create(
                    endpoint=endpoint,
                    name=data.get("name", f"Test {index}"),
                    description=data.get("description", ""),
                    priority=data.get("priority", "medium").lower(),
                    category=data.get("category", "functional"),
                    layer=data.get("layer", "backend"),
                    runner_type=data.get("runner_type", "http"),
                    test_script=data.get("test_script"),
                    headers=data.get("headers", {}),
                    query_params=data.get("query_params", {}),
                    body=data.get("body", {}),
                    expected_status=data.get("expected_status", 200),
                    assertions=data.get("assertions", []),
                    tags=data.get("tags", []),
                    use_visual_ai=data.get("use_visual_ai", False),
                    ai_generated=data.get("ai_generated", True), # Default to true since this flow is "Batch AI"
                    user_story=data.get("user_story", "") 
                )
                created_tests.append(test_case)
            except Exception as e:
                 errors.append({"index": index, "error": str(e)})

        # Handle Auto-Run
        if should_auto_run and created_tests:
            from billing.services import deduct_tokens, calculate_test_cost
            total_auto_run_cost = sum(calculate_test_cost(test.runner_type) for test in created_tests)
            
            can_auto_run = deduct_tokens(
                request.user, 
                total_auto_run_cost, 
                f"Batch Auto-Run: {len(created_tests)} tests"
            )
            
            if can_auto_run:
                import uuid
                batch_id = str(uuid.uuid4())
                for test in created_tests:
                    if HAS_CELERY:
                        try:
                            run_test_case_task.delay(test.id, batch_id=batch_id, triggered_by="manual", send_notification=False)
                        except Exception as e:
                            logger.error(f"Failed to queue task: {e}. Falling back to sync.")
                            RunnerService().execute_test(test.id, batch_id=batch_id, triggered_by="manual")
                    else:
                        RunnerService().execute_test(test.id, batch_id=batch_id, triggered_by="manual")
                
                # Schedule batch report
                from .tasks import send_batch_report_task
                send_batch_report_task.apply_async((batch_id, request.user.id), countdown=60)
            else:
                balance = request.user.token_balance
                if balance <= 0:
                    error_msg = "You have no tokens available for auto-run. Please top up your balance. Tests were created but not run."
                else:
                    error_msg = f"Insufficient tokens for auto-run. Required: {total_auto_run_cost}, Balance: {balance}. Tests were created but not run."
                errors.append({"error": error_msg})


        serializer = TestCaseSerializer(created_tests, many=True)
        resp_data = {
            "created": serializer.data,
            "errors": errors
        }
        
        # Determine status
        if not created_tests and errors:
            return Response(resp_data, status=400)
            
        return Response(resp_data, status=201)



class TestCaseDetailView(generics.RetrieveUpdateDestroyAPIView):
    serializer_class = TestCaseSerializer
    permission_classes = [permissions.IsAuthenticated]
    queryset = TestCase.objects.all()

class RunTestView(APIView):
    permission_classes = [permissions.IsAuthenticated]
    
    @method_decorator(ratelimit(key='user', rate='5/m', method='POST'))
    def post(self, request, pk):
        test_case = get_object_or_404(TestCase, id=pk, collection__project__user=request.user)
        run_async = request.data.get("async", True) and HAS_CELERY
        
        # Billing: Deduct Tokens
        from billing.services import deduct_tokens, calculate_test_cost
        cost = calculate_test_cost(test_case.runner_type)
        
        if not deduct_tokens(request.user, cost, f"Run Test: {test_case.name}"):
             balance = request.user.token_balance
             if balance <= 0:
                 error_msg = "You have no tokens available. Please top up your balance to run tests."
             else:
                 error_msg = f"Insufficient tokens. Required: {cost}, Balance: {balance}."
             return Response({"error": error_msg}, status=402)
        
        if run_async:
            try:
                task = run_test_case_task.delay(test_case.id)
                return Response({"message": "Test queued", "task_id": task.id}, status=status.HTTP_202_ACCEPTED)
            except Exception as e:
                logger.error(f"Celery error: {e}. Falling back to sync run.")
        
        runner = RunnerService()
        result = runner.execute_test(test_case.id)
        return Response({
            "status": result.status,
            "response_status": result.response_status,
            "response_time": result.response_time_ms,
            "error": result.error_message,
            "logs": result.logs,
            "run_id": result.id
        }, status=status.HTTP_200_OK)




class TriggerTestRunView(APIView):
    """
    Webhook endpoint to trigger test runs from CIs (GitHub Actions, etc.)
    """
    permission_classes = [permissions.IsAuthenticated]

    @swagger_auto_schema(
        operation_description="Trigger test runs via webhook",
        request_body=openapi.Schema(
            type=openapi.TYPE_OBJECT,
            required=['project_id'],
            properties={
                'project_id': openapi.Schema(type=openapi.TYPE_INTEGER),
                'collection_id': openapi.Schema(type=openapi.TYPE_INTEGER, description="Optional: specific collection"),
                'target_url': openapi.Schema(type=openapi.TYPE_STRING, description="Override base URL for this run (e.g. staging deployment)")
            }
        ),
        responses={
            202: "Tests Queued"
        }
    )
    def post(self, request):
        project_id = request.data.get('project_id')
        collection_id = request.data.get('collection_id')
        target_url = request.data.get('target_url')
        
        if not project_id:
            return Response({'error': 'project_id is required'}, status=status.HTTP_400_BAD_REQUEST)
            
        # Verify ownership
        project = get_object_or_404(Project, id=project_id, user=request.user)
        
        # Build Query
        test_cases = TestCase.objects.filter(collection__project=project)
        if collection_id:
            test_cases = test_cases.filter(collection_id=collection_id)
            
        if not test_cases.exists():
            return Response({'message': 'No tests found to run'}, status=status.HTTP_404_NOT_FOUND)
            
        # Billing: Calculate Total Cost
        from billing.services import deduct_tokens, calculate_test_cost
        total_cost = sum(calculate_test_cost(tc.runner_type) for tc in test_cases)
        
        if not deduct_tokens(request.user, total_cost, f"Webhook Trigger: {project.name}"):
            balance = request.user.token_balance
            if balance <= 0:
                error_msg = "You have no tokens available. Please top up your balance."
            else:
                error_msg = f"Insufficient tokens. Required: {total_cost}, Balance: {balance}."
            return Response({'error': error_msg}, status=status.HTTP_402_PAYMENT_REQUIRED)

        # Enqueue runs
        run_ids = []
        if HAS_CELERY:
            import uuid
            batch_id = str(uuid.uuid4())
            for tc in test_cases:
                task = run_test_case_task.delay(
                    tc.id, 
                    override_url=target_url, 
                    batch_id=batch_id, 
                    triggered_by="webhook",
                    send_notification=False
                )
                run_ids.append(task.id)
            
            # Schedule batch report
            from .tasks import send_batch_report_task
            send_batch_report_task.apply_async((batch_id, request.user.id), countdown=120)
                
            return Response({
                'message': f'Queued {len(run_ids)} tests (Total Cost: {total_cost})',
                'task_ids': run_ids,
                'batch_id': batch_id
            }, status=status.HTTP_202_ACCEPTED)
        else:
            return Response({'error': 'Async runner not available'}, status=status.HTTP_501_NOT_IMPLEMENTED)



class TestRunListView(generics.ListAPIView):
    serializer_class = TestRunSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        test_case_id = self.kwargs.get("test_case_id")
        endpoint_id = self.request.query_params.get("endpoint_id")
        collection_id = self.request.query_params.get("collection_id")
        batch_id = self.request.query_params.get("batch_id")
        status_filter = self.request.query_params.get("status")
        start_date = self.request.query_params.get("start_date")
        end_date = self.request.query_params.get("end_date")
        
        queryset = TestRun.objects.filter(test_case__endpoint__collection__project__user=self.request.user)
        
        if test_case_id:
            queryset = queryset.filter(test_case__id=test_case_id)
        
        if endpoint_id:
            queryset = queryset.filter(test_case__endpoint__id=endpoint_id)
        
        if collection_id:
            queryset = queryset.filter(test_case__endpoint__collection__id=collection_id)
        
        if batch_id:
            queryset = queryset.filter(batch_id=batch_id)

        if status_filter:
            queryset = queryset.filter(status=status_filter)
            
        if start_date:
            queryset = queryset.filter(executed_at__gte=start_date)
            
        if end_date:
            queryset = queryset.filter(executed_at__lte=end_date)
            
        return queryset.order_by('-executed_at')

class TestRunDetailView(generics.RetrieveAPIView):
    serializer_class = TestRunSerializer
    permission_classes = [permissions.IsAuthenticated]
    
    def get_queryset(self):
        return TestRun.objects.filter(test_case__endpoint__collection__project__user=self.request.user)


class ProjectRunReportsView(APIView):
    """
    Unified, project-scoped test run report.

    Returns every test run executed under a project (optionally narrowed to a
    single collection and/or status), enriched with the QA-standard fields:
    root-cause classification per failure and a per-test flakiness flag based
    on that test's recent history. Powers the project-level Reports hub.

    GET /test-cases/projects/<project_id>/reports/runs/

    Query params:
    - collection: UUID of a collection to narrow to
    - status: passed | failed | error | running | pending
    """
    permission_classes = [permissions.IsAuthenticated]

    @swagger_auto_schema(
        operation_description="Get all test runs under a project/collection with QA analysis",
        manual_parameters=[
            openapi.Parameter('collection', openapi.IN_QUERY, type=openapi.TYPE_STRING),
            openapi.Parameter('status', openapi.IN_QUERY, type=openapi.TYPE_STRING),
        ],
        responses={200: "Project run report JSON"}
    )
    def get(self, request, project_id):
        project = get_object_or_404(Project, id=project_id, user=request.user)
        collection_id = request.query_params.get('collection')
        status_filter = request.query_params.get('status')

        qs = TestRun.objects.filter(
            test_case__endpoint__collection__project=project
        ).select_related(
            'test_case', 'test_case__endpoint', 'test_case__endpoint__collection'
        )
        if collection_id:
            qs = qs.filter(test_case__endpoint__collection_id=collection_id)
        if status_filter:
            qs = qs.filter(status=status_filter)

        total = qs.count()
        MAX_ROWS = 1000
        runs = list(qs.order_by('-executed_at')[:MAX_ROWS])

        passed = sum(1 for r in runs if r.status == 'passed')
        failed = sum(1 for r in runs if r.status in ('failed', 'error'))
        running = sum(1 for r in runs if r.status == 'running')
        pending = sum(1 for r in runs if r.status == 'pending')
        avg_time = (
            sum(r.response_time_ms or 0 for r in runs) / len(runs)
            if runs else 0
        )

        # Flakiness is a property of the test (did it flip pass/fail recently?),
        # so memoize one history lookup per distinct test case in the window.
        flaky_cache = {}
        def _is_flaky(tc_id):
            if tc_id not in flaky_cache:
                flaky_cache[tc_id] = flaky_summary(tc_id, limit=10)['flaky']
            return flaky_cache[tc_id]

        rows = []
        for r in runs:
            row = {
                'id': r.id,
                'test_case_id': r.test_case_id,
                'test_case_name': r.test_case.name if r.test_case else None,
                'collection_id': r.test_case.endpoint.collection_id if r.test_case and r.test_case.endpoint else None,
                'collection_name': r.test_case.endpoint.collection.name if r.test_case and r.test_case.endpoint else None,
                'endpoint_method': r.test_case.endpoint.method if r.test_case and r.test_case.endpoint else None,
                'endpoint_url': r.test_case.endpoint.url if r.test_case and r.test_case.endpoint else None,
                'status': r.status,
                'response_status': r.response_status,
                'response_time_ms': r.response_time_ms,
                'triggered_by': r.triggered_by,
                'batch_id': str(r.batch_id) if r.batch_id else None,
                'executed_at': r.executed_at.isoformat() if r.executed_at else None,
                'flaky': _is_flaky(r.test_case_id),
            }
            if r.status in ('failed', 'error'):
                row['classification'] = classify_failure(r.error_message, r.response_status)
                row['error_message'] = (r.error_message or '')[:600]
            rows.append(row)

        flaky_runs = sum(1 for r in rows if r['flaky'] and r['status'] in ('failed', 'error'))
        pass_rate = round((passed / total) * 100, 1) if total else 0.0

        return Response({
            'project_id': str(project.id),
            'project_name': project.name,
            'collection_id': collection_id,
            'summary': {
                'total_runs': total,
                'passed': passed,
                'failed': failed,
                'running': running,
                'pending': pending,
                'flaky_failures': flaky_runs,
                'pass_rate': pass_rate,
                'avg_response_time_ms': round(avg_time, 1),
                'truncated': total > MAX_ROWS,
            },
            'runs': rows,
        })


class ProjectStatusView(APIView):
    """
    Returns an aggregated overview of the project's health.
    Shows failure counts, total tests, and the latest status of each endpoint.
    """
    permission_classes = [permissions.IsAuthenticated]

    @swagger_auto_schema(
        operation_description="Get project-wide test status summary",
        responses={200: "Health Summary JSON"}
    )
    @method_decorator(cache_page(60 * 2)) # Cache for 2 mins
    def get(self, request, project_id):
        project = get_object_or_404(Project, id=project_id, user=request.user)
        
        # Get all test cases for this project
        test_cases = TestCase.objects.filter(endpoint__collection__project=project)
        
        total_tests = test_cases.count()
        if total_tests == 0:
            return Response({
                "project_name": project.name,
                "summary": {"total": 0, "passed": 0, "failed": 0, "pass_rate": 0},
                "endpoints": []
            })

        # Group by Endpoint — all done in 4 queries total (endpoints, test_cases, runs, done)
        from collection.models import Endpoint
        from django.db.models import Max
        from django.db import connection
        
        endpoints = list(Endpoint.objects.filter(collection__project=project).select_related('collection'))
        
        # Build lookups: {test_case_id: endpoint_id} AND {test_case_id: {name, category, priority}}
        test_to_endpoint = {}
        test_info = {}
        for t in test_cases.only('id', 'endpoint_id', 'name', 'category', 'priority'):
            test_to_endpoint[t.id] = t.endpoint_id
            test_info[t.id] = {'name': t.name, 'category': t.category, 'priority': t.priority}
        test_cases_by_endpoint = {}
        for tc_id, ep_id in test_to_endpoint.items():
            test_cases_by_endpoint.setdefault(ep_id, []).append(tc_id)
        
        # Fetch latest run per test_case in ONE query using raw SQL window function
        latest_run_map = {}
        if test_to_endpoint:
            tc_ids = tuple(test_to_endpoint.keys())
            _LATEST_RUN_SQL = (
                "SELECT test_case_id, id, status, executed_at FROM ("
                "SELECT test_case_id, id, status, executed_at, "
                "ROW_NUMBER() OVER (PARTITION BY test_case_id ORDER BY executed_at DESC) as rn "
                "FROM test_cases_testrun "
                "WHERE test_case_id IN %s"
                ") sub WHERE rn = 1"
            )
            try:
                with connection.cursor() as cursor:
                    cursor.execute(_LATEST_RUN_SQL, [tc_ids if len(tc_ids) > 1 else (tc_ids[0],)])
                    for row in cursor.fetchall():
                        latest_run_map[row[0]] = {'id': row[1], 'status': row[2], 'executed_at': row[3]}
            except Exception:
                # Fallback: annotate + Max (still far better than N+1)
                latest_ids = (
                    TestRun.objects.filter(test_case_id__in=tc_ids)
                    .values('test_case_id')
                    .annotate(latest_id=Max('id'))
                    .values_list('latest_id', flat=True)
                )
                for run in TestRun.objects.filter(id__in=latest_ids):
                    latest_run_map[run.test_case_id] = {'id': run.id, 'status': run.status, 'executed_at': run.executed_at}
        
        # Compute summary counts from the map
        passed_count = sum(1 for r in latest_run_map.values() if r['status'] == 'passed')
        failed_count = sum(1 for r in latest_run_map.values() if r['status'] in ('failed', 'error'))
        
        endpoint_status = []
        for endpoint in endpoints:
            tc_ids = test_cases_by_endpoint.get(endpoint.id, [])
            tests_list = []
            endpoint_failed = 0
            has_runs = False
            
            for tc_id in tc_ids:
                latest_run = latest_run_map.get(tc_id)
                info = test_info.get(tc_id, {})
                run_status = "no_runs"
                last_run_id = None
                last_run_at = None
                if latest_run:
                    run_status = latest_run['status']
                    last_run_id = latest_run['id']
                    last_run_at = latest_run['executed_at']
                    has_runs = True
                    if run_status in ["failed", "error"]:
                        endpoint_failed += 1
                
                tests_list.append({
                    "id": tc_id,
                    "name": info.get('name', ''),
                    "status": run_status,
                    "last_run_id": last_run_id,
                    "last_run_at": last_run_at,
                    "category": info.get('category', ''),
                    "priority": info.get('priority', '')
                })

            endpoint_status.append({
                "id": endpoint.id,
                "name": endpoint.name,
                "method": endpoint.method,
                "url": endpoint.url,
                "collection_id": endpoint.collection.id,
                "collection_name": endpoint.collection.name,
                "total_tests": len(tc_ids),
                "failed_tests": endpoint_failed,
                "status": "failing" if endpoint_failed > 0 else "passing" if has_runs else "no_runs",
                "tests": tests_list
            })

        return Response({
            "project_name": project.name,
            "project_id": str(project.id),
            "summary": {
                "total": total_tests,
                "passed": passed_count,
                "failed": failed_count,
                "pass_rate": round((passed_count / total_tests) * 100, 2) if total_tests > 0 else 0
            },
            "endpoints": endpoint_status
        })


class ProjectAutoPilotView(APIView):
    """
    Triggers AI generation and execution for all endpoints in a project.
    """
    permission_classes = [permissions.IsAuthenticated]

    @swagger_auto_schema(
        operation_description="Trigger AI Auto-Pilot for an entire project",
        request_body=openapi.Schema(
            type=openapi.TYPE_OBJECT,
            properties={
                'scenarios': openapi.Schema(type=openapi.TYPE_ARRAY, items=openapi.Items(type=openapi.TYPE_STRING), 
                    description="Standard scenarios to generate for each endpoint (e.g. ['HAPPY_PATH'])"),
                'user_story': openapi.Schema(type=openapi.TYPE_STRING, description="Optional: Context or requirements that apply to all endpoints in this batch"),
                'runner_types': openapi.Schema(type=openapi.TYPE_ARRAY, items=openapi.Items(type=openapi.TYPE_STRING, enum=['http', 'load', 'browser']), default=['http']),
                'categories': openapi.Schema(type=openapi.TYPE_ARRAY, items=openapi.Items(type=openapi.TYPE_STRING), default=['functional']),
                'layer': openapi.Schema(type=openapi.TYPE_STRING, default='backend'),
                'use_visual_ai': openapi.Schema(type=openapi.TYPE_BOOLEAN, default=False)
            }
        ),
        responses={202: "Auto-Pilot started"}
    )
    def post(self, request, project_id):
        project = get_object_or_404(Project, id=project_id, user=request.user)
        scenarios = request.data.get("scenarios", ["HAPPY_PATH", "VALIDATION_ERROR", "SECURITY"])
        user_story = request.data.get("user_story", "")
        
        # Parse lists directly, fallback to single legacy keys if needed
        runner_types = request.data.get("runner_types", [])
        if not runner_types:
             single = request.data.get("runner_type", "http")
             runner_types = [single]

        categories = request.data.get("categories", [])
        if not categories:
             single = request.data.get("category", "functional")
             categories = [single]

        layer = request.data.get("layer", "backend")
        use_visual_ai = request.data.get("use_visual_ai", False)
        
        if not HAS_CELERY:
            return Response({"error": "Auto-Pilot requires Celery for background processing."}, status=501)

        # Billing: Upfront check
        from billing.services import calculate_test_cost
        AGENT_ENTRY_COST = calculate_test_cost('agent_mission_entry')
        if request.user.token_balance < AGENT_ENTRY_COST:
            return Response({"error": f"Insufficient tokens for Auto-Pilot. Required: {AGENT_ENTRY_COST}, Balance: {request.user.token_balance}."}, status=402)

        import uuid
        batch_id = uuid.uuid4()
        
        from .models import AgentMission
        
        # We need a collection to link to if possible
        first_collection = project.collections.first()
        if not first_collection:
             return Response({"error": "Project must have at least one collection (import Swagger first)."}, status=400)

        # Detect Safe Mode: True if any endpoint starts with a production URL or if explicitly requested
        is_safe_mode = request.data.get("is_safe_mode", True)
        
        mission = AgentMission.objects.create(
            user=request.user,
            collection=first_collection,
            user_story=user_story or f"Perform a comprehensive QA mission for {project.name}. Verify all core features.",
            mission_type="qa_testing",
            batch_id=batch_id,
            browser_config=request.data.get("browser_config", {}),
            is_safe_mode=is_safe_mode,
            scenarios=scenarios,
            categories=categories
        )

        # AUTO-LINK REGRESSION BASELINE
        if any(s.upper().replace(' ', '_').replace('-', '_') == 'REGRESSION' for s in (scenarios or [])):
            last_completed = AgentMission.objects.filter(
                collection=first_collection,
                status__in=['completed', 'error'],
                id__lt=mission.id
            ).order_by('-created_at').first()
            if last_completed:
                mission.previous_session = last_completed
                mission.save(update_fields=['previous_session'])
                logger.info(f"[ProjectAutoPilot] Auto-linked regression baseline: {last_completed.batch_id}")

        from .tasks import run_autonomous_mission_task
        run_autonomous_mission_task.delay(
            mission_id=mission.id,
            user_id=request.user.id
        )

        return Response({
            "message": "Project Auto-Pilot Mission started.",
            "batch_id": str(batch_id),
            "mission_id": mission.id
        }, status=status.HTTP_202_ACCEPTED)


class ProjectSecurityAuditView(APIView):
    """
    Triggers a specialized Security Pentesting Mission for a project.
    """
    permission_classes = [permissions.IsAuthenticated]

    @swagger_auto_schema(
        operation_description="Trigger a Security Audit for an entire project",
        request_body=openapi.Schema(
            type=openapi.TYPE_OBJECT,
            properties={
                'browser_config': openapi.Schema(type=openapi.TYPE_OBJECT, description="Optional browser configuration")
            }
        ),
        responses={202: "Security Audit started"}
    )
    def post(self, request, project_id):
        project = get_object_or_404(Project, id=project_id, user=request.user)
        browser_config = request.data.get("browser_config", {})

        from .tasks import HAS_CELERY
        if not HAS_CELERY:
            return Response({"error": "Security Audits require Celery."}, status=501)

        from billing.services import calculate_test_cost
        SECURITY_AUDIT_ENTRY = calculate_test_cost('security_audit_entry')

        if request.user.token_balance < SECURITY_AUDIT_ENTRY:
            return Response({"error": f"Insufficient tokens for a Security Audit ({SECURITY_AUDIT_ENTRY} tokens required to start)."}, status=402)

        import uuid
        batch_id = uuid.uuid4()
        
        # We need a collection to link to if possible, or handle NULL in models (currently required)
        first_collection = project.collections.first()
        if not first_collection:
             return Response({"error": "Project must have at least one collection (import Swagger first)."}, status=400)

        from .models import AgentMission
        mission = AgentMission.objects.create(
            user=request.user,
            collection=first_collection,
            user_story=f"SECURITY AUDIT: Systematically identify vulnerabilities across {project.name}. Focus on XSS, SQLi, and Auth Bypass.",
            mission_type="security_audit",
            batch_id=batch_id,
            browser_config=browser_config
        )

        from .tasks import run_autonomous_mission_task
        run_autonomous_mission_task.delay(
            mission_id=mission.id,
            user_id=request.user.id
        )

        return Response({
            "message": "Security Audit queued.",
            "batch_id": str(batch_id)
        }, status=status.HTTP_202_ACCEPTED)


class CollectionAutoPilotView(APIView):
    """
    Triggers AI generation and execution for all endpoints in a specific collection.
    More granular than project-level auto-pilot.
    """
    permission_classes = [permissions.IsAuthenticated]

    @swagger_auto_schema(
        operation_description="Trigger AI Auto-Pilot for a specific collection",
        request_body=openapi.Schema(
            type=openapi.TYPE_OBJECT,
            properties={
                'scenarios': openapi.Schema(type=openapi.TYPE_ARRAY, items=openapi.Items(type=openapi.TYPE_STRING), 
                    description="Standard scenarios to generate for each endpoint (e.g. ['HAPPY_PATH'])"),
                'user_story': openapi.Schema(type=openapi.TYPE_STRING, description="Optional: Context or requirements for this collection"),
                'runner_types': openapi.Schema(type=openapi.TYPE_ARRAY, items=openapi.Items(type=openapi.TYPE_STRING, enum=['http', 'load', 'browser']), default=['http']),
                'categories': openapi.Schema(type=openapi.TYPE_ARRAY, items=openapi.Items(type=openapi.TYPE_STRING), default=['functional']),
                'layer': openapi.Schema(type=openapi.TYPE_STRING, default='backend'),
                'use_visual_ai': openapi.Schema(type=openapi.TYPE_BOOLEAN, default=False)
            }
        ),
        responses={202: "Auto-Pilot started"}
    )
    def post(self, request, collection_id):
        from collection.models import Collection
        collection = get_object_or_404(Collection, id=collection_id, project__user=request.user)
        
        scenarios = request.data.get("scenarios", ["HAPPY_PATH", "VALIDATION_ERROR", "SECURITY"])
        user_story = request.data.get("user_story", "")
        
        # Parse lists directly, fallback to collection defaults based on source
        runner_types = request.data.get("runner_types", [])
        if not runner_types:
             # Default runner_type based on collection source
             if collection.source in ['browser', 'crawler']:
                 runner_types = ['browser', 'http']
             else:
                 single = request.data.get("runner_type", "http")
                 runner_types = [single]

        categories = request.data.get("categories", [])
        if not categories:
             single = request.data.get("category", "functional")
             categories = [single]

        layer = request.data.get("layer", "backend")
        use_visual_ai = request.data.get("use_visual_ai", False)
        
        # Auto-infer runner_types from layer if not provided
        runner_types = request.data.get("runner_types", [])
        if not runner_types:
            if layer == "fullstack":
                runner_types = ["http", "browser", "mail"]
            elif layer == "frontend":
                runner_types = ["browser", "mail"]
            else: # backend
                runner_types = ["http", "mail"]

        if not HAS_CELERY:
            return Response({"error": "Auto-Pilot requires Celery for background processing."}, status=501)

        # Billing: Upfront check
        from billing.services import calculate_test_cost
        AGENT_ENTRY_COST = calculate_test_cost('agent_mission_entry')
        if request.user.token_balance < AGENT_ENTRY_COST:
            return Response({"error": f"Insufficient tokens for Auto-Pilot. Required: {AGENT_ENTRY_COST}, Balance: {request.user.token_balance}."}, status=402)

        import uuid
        batch_id = uuid.uuid4()
        
        # Create Agent Mission RECORD IMMEDIATELY (Avoids Frontend 404 while polling)
        from .models import AgentMission
        final_story = user_story or collection.user_story or collection.description or collection.project.user_story or collection.project.description or ""
        
        # Persist browser config so the worker can pick it up
        browser_config = request.data.get("browser_config", {})
        is_safe_mode = request.data.get("is_safe_mode", True)

        mission = AgentMission.objects.create(
            user=request.user,
            collection=collection,
            user_story=final_story,
            batch_id=str(batch_id),
            mission_type="qa_testing",
            browser_config=browser_config,
            is_safe_mode=is_safe_mode,
            status="running",
            scenarios=scenarios if isinstance(scenarios, list) else [scenarios],
            categories=categories
        )

        # AUTO-LINK REGRESSION BASELINE: If REGRESSION is in scenarios,
        # find the last completed session for this collection and link it
        effective_scenarios = scenarios if isinstance(scenarios, list) else [scenarios]
        if any(s.upper().replace(' ', '_').replace('-', '_') == 'REGRESSION' for s in effective_scenarios):
            last_completed = AgentMission.objects.filter(
                collection=collection,
                status__in=['completed', 'error'],
                id__lt=mission.id
            ).order_by('-created_at').first()
            if last_completed:
                mission.previous_session = last_completed
                mission.save(update_fields=['previous_session'])
                logger.info(f"[CollectionAutoPilot] Auto-linked regression baseline: {last_completed.batch_id}")

        from .tasks import collection_auto_pilot_task
        
        logger.info(f"[CollectionAutoPilotView] Triggering task for mission {mission.id} with batch_id {batch_id}")
        
        try:
            task = collection_auto_pilot_task.delay(
                collection_id=str(collection.id),
                user_id=request.user.id,
                scenarios=scenarios,
                batch_id=str(batch_id),
                user_story=user_story,
                runner_types=runner_types,
                categories=categories,
                layer=layer,
                use_visual_ai=use_visual_ai,
                mission_id=mission.id
            )
            logger.info(f"[CollectionAutoPilotView] Task {task.id} queued successfully")
        except Exception as e:
            logger.error(f"[CollectionAutoPilotView] Failed to queue task: {e}")
            mission.status = "error"
            mission.error_message = str(e)
            mission.save()
            return Response({"error": "Failed to queue background task"}, status=500)

        return Response({
            "message": "Collection Auto-Pilot started successfully",
            "batch_id": batch_id,
            "task_id": task.id,
            "description": f"Generating and running tests for all endpoints in collection '{collection.name}'."
        }, status=status.HTTP_202_ACCEPTED)


class CollectionStatusView(APIView):
    """
    Returns status summary for a specific collection.
    Shows all endpoints in the collection and their test results.
    """
    permission_classes = [permissions.IsAuthenticated]

    @swagger_auto_schema(
        operation_description="Get collection-level test status summary",
        responses={200: "Collection Health Summary JSON"}
    )
    @method_decorator(cache_page(60 * 1)) # Cache for 1 min
    def get(self, request, collection_id):
        from collection.models import Collection, Endpoint
        collection = get_object_or_404(Collection, id=collection_id, project__user=request.user)
        
        # Get all endpoints in this collection
        endpoints = Endpoint.objects.filter(collection=collection)
        
        # Get all test cases for this collection
        test_cases = TestCase.objects.filter(endpoint__collection=collection)
        
        total_tests = test_cases.count()
        if total_tests == 0:
            return Response({
                "collection_name": collection.name,
                "collection_id": str(collection.id),
                "project_name": collection.project.name,
                "preferred_test_types": collection.project.preferred_test_types or [],
                "summary": {"total": 0, "passed": 0, "failed": 0, "pass_rate": 0},
                "endpoints": []
            })
        
        # Group by Endpoint — all done in ~4 queries total
        from django.db.models import Max
        from django.db import connection
        
        endpoints = list(endpoints.select_related('collection'))
        
        _LATEST_RUN_SQL = (
            "SELECT test_case_id, id, status, executed_at FROM ("
            "SELECT test_case_id, id, status, executed_at, "
            "ROW_NUMBER() OVER (PARTITION BY test_case_id ORDER BY executed_at DESC) as rn "
            "FROM test_cases_testrun "
            "WHERE test_case_id IN %s"
            ") sub WHERE rn = 1"
        )
        # Build lookups: {test_case_id: endpoint_id} AND {test_case_id: {name, category, priority}}
        test_to_endpoint = {}
        test_info = {}
        for t in test_cases.only('id', 'endpoint_id', 'name', 'category', 'priority'):
            test_to_endpoint[t.id] = t.endpoint_id
            test_info[t.id] = {'name': t.name, 'category': t.category, 'priority': t.priority}
        test_cases_by_endpoint = {}
        for tc_id, ep_id in test_to_endpoint.items():
            test_cases_by_endpoint.setdefault(ep_id, []).append(tc_id)
        
        # Fetch latest run per test_case in ONE query using raw SQL window function
        latest_run_map = {}
        if test_to_endpoint:
            tc_ids = tuple(test_to_endpoint.keys())
            try:
                with connection.cursor() as cursor:
                    cursor.execute(_LATEST_RUN_SQL, [tc_ids if len(tc_ids) > 1 else (tc_ids[0],)])
                    for row in cursor.fetchall():
                        latest_run_map[row[0]] = {'id': row[1], 'status': row[2], 'executed_at': row[3]}
            except Exception:
                latest_ids = (
                    TestRun.objects.filter(test_case_id__in=tc_ids)
                    .values('test_case_id')
                    .annotate(latest_id=Max('id'))
                    .values_list('latest_id', flat=True)
                )
                for run in TestRun.objects.filter(id__in=latest_ids):
                    latest_run_map[run.test_case_id] = {'id': run.id, 'status': run.status, 'executed_at': run.executed_at}
        
        passed_count = sum(1 for r in latest_run_map.values() if r['status'] == 'passed')
        failed_count = sum(1 for r in latest_run_map.values() if r['status'] in ('failed', 'error'))
        
        endpoint_status = []
        for endpoint in endpoints:
            tc_ids = test_cases_by_endpoint.get(endpoint.id, [])
            tests_list = []
            endpoint_failed = 0
            has_runs = False
            
            for tc_id in tc_ids:
                latest_run = latest_run_map.get(tc_id)
                info = test_info.get(tc_id, {})
                run_status = "no_runs"
                last_run_id = None
                last_run_at = None
                if latest_run:
                    run_status = latest_run['status']
                    last_run_id = latest_run['id']
                    last_run_at = latest_run['executed_at']
                    has_runs = True
                    if run_status in ["failed", "error"]:
                        endpoint_failed += 1
                
                tests_list.append({
                    "id": tc_id,
                    "name": info.get('name', ''),
                    "status": run_status,
                    "last_run_id": last_run_id,
                    "last_run_at": last_run_at,
                    "category": info.get('category', ''),
                    "priority": info.get('priority', '')
                })

            endpoint_status.append({
                "id": endpoint.id,
                "name": endpoint.name,
                "method": endpoint.method,
                "url": endpoint.url,
                "total_tests": len(tc_ids),
                "failed_tests": endpoint_failed,
                "status": "failing" if endpoint_failed > 0 else "passing" if has_runs else "no_runs",
                "tests": tests_list
            })

        return Response({
            "collection_name": collection.name,
            "collection_id": str(collection.id),
            "project_name": collection.project.name,
            "project_id": str(collection.project.id),
            "preferred_test_types": collection.project.preferred_test_types or [],
            "summary": {
                "total": total_tests,
                "passed": passed_count,
                "failed": failed_count,
                "pass_rate": round((passed_count / total_tests) * 100, 2) if total_tests > 0 else 0
            },
            "endpoints": endpoint_status
        })

class AgentMissionListView(generics.ListAPIView):
    serializer_class = AgentMissionSerializer
    permission_classes = [permissions.IsAuthenticated]

    # No caching - missions update frequently and we need fresh status
    def get_queryset(self):
        return AgentMission.objects.filter(user=self.request.user).order_by('-created_at')

class AgentMissionDetailView(generics.RetrieveAPIView):
    serializer_class = AgentMissionSerializer
    permission_classes = [permissions.IsAuthenticated]
    
    def get_queryset(self):
        return AgentMission.objects.filter(user=self.request.user)

    def get_object(self):
        batch_id = self.kwargs.get("batch_id")
        if batch_id:
             return get_object_or_404(AgentMission, batch_id=batch_id, user=self.request.user)
        return super().get_object()

class AgentMissionPromptView(APIView):
    """
    Allows user to send a guidance prompt to a running mission.
    """
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, batch_id):
        mission = get_object_or_404(AgentMission, batch_id=batch_id, user=request.user)
        prompt_text = request.data.get("prompt")
        
        if not prompt_text:
            return Response({"error": "Prompt required"}, status=400)
            
        prompt = AgentPrompt.objects.create(
            mission=mission,
            prompt=prompt_text
        )
        
        return Response(AgentPromptSerializer(prompt).data, status=201)



class SessionReportView(APIView):
    """
    Returns a structured, machine-readable report for a completed session.
    Designed for both human UI consumption and programmatic agent access.
    
    GET /test-cases/sessions/<batch_id>/report/
    
    The report includes:
    - Executive summary (pass rate, step counts, duration)
    - All mission steps with full details
    - Regression analysis against the previous session (if linked)
    - The user story and mission context
    """
    permission_classes = [permissions.IsAuthenticated]

    @swagger_auto_schema(
        operation_description="Get a structured session report for a completed mission",
        responses={200: "Session Report JSON", 404: "Session not found"}
    )
    def get(self, request, batch_id):
        mission = get_object_or_404(
            AgentMission, batch_id=batch_id, user=request.user
        )
        serializer = SessionReportSerializer(mission)
        data = serializer.data

        # --- QA-standard analysis --------------------------------------------------
        # Enrich with a TestRun summary for this batch, plus a root-cause / flakiness
        # analysis of every test executed under this session.
        runs = list(TestRun.objects.filter(batch_id=batch_id).select_related(
            'test_case', 'test_case__endpoint'
        ))
        total_runs = len(runs)
        passed_runs = sum(1 for r in runs if r.status == 'passed')
        failed_runs = sum(1 for r in runs if r.status in ('failed', 'error'))
        avg_time = sum(r.response_time_ms or 0 for r in runs) / total_runs if total_runs else 0

        test_analysis = analyze_runs(runs) if runs else []
        flaky_tests = [t for t in test_analysis if t['flaky']]
        run_breakdown = failure_breakdown(runs)
        run_pass_rate = round((passed_runs / total_runs) * 100, 1) if total_runs else 0.0

        data['run_summary'] = {
            'total_runs': total_runs,
            'passed_runs': passed_runs,
            'failed_runs': failed_runs,
            'avg_response_time_ms': round(avg_time, 1),
            'triggered_by': (runs[0].triggered_by if runs else 'unknown'),
            'pass_rate': run_pass_rate,
            'flaky_tests': len(flaky_tests),
            'failure_breakdown': run_breakdown,
            'qa_verdict': qa_verdict(run_pass_rate, len(flaky_tests)),
        }
        data['test_analysis'] = test_analysis

        # Tag every failed step with a root-cause classification so the UI and
        # any downstream consumer can reason about *why* it failed.
        for step in data.get('steps', []):
            if step.get('status') in ('failed', 'error'):
                text = (step.get('thought') or '') + ' ' + (step.get('response_body') or '')[:500]
                step['classification'] = classify_failure(
                    text, step.get('response_status')
                )

        # If there's a previous session, include a comparison summary
        if mission.previous_session:
            prev = mission.previous_session
            data['comparison'] = {
                'previous_session_id': str(prev.batch_id),
                'previous_status': prev.status,
                'previous_pass_rate': prev.pass_rate,
                'current_pass_rate': mission.pass_rate,
                'regressions_found': len(mission.regressions),
                'regressions': mission.regressions,
                'improvements': self._find_improvements(prev, mission),
            }
        else:
            data['comparison'] = None

        return Response(data)
    
    def _find_improvements(self, prev_mission, curr_mission):
        """Steps that failed in the previous session but now pass."""
        prev_failed = set(
            prev_mission.steps.filter(status='failed').values_list('action_type', flat=True)
        )
        curr_passed = curr_mission.steps.filter(status='passed')
        return [
            {'step_number': s.step_number, 'action_type': s.action_type, 'thought': s.thought}
            for s in curr_passed if s.action_type in prev_failed
        ]


class SessionHistoryView(APIView):
    """
    Lists all past sessions for a collection, ordered by most recent.
    Lightweight — does not include full step details.
    
    GET /test-cases/collections/<collection_id>/sessions/
    
    Query params:
    - limit: Max results (default 50)
    - status: Filter by mission status (completed, error, etc.)
    """
    permission_classes = [permissions.IsAuthenticated]

    @swagger_auto_schema(
        operation_description="List past sessions for a collection",
        manual_parameters=[
            openapi.Parameter('limit', openapi.IN_QUERY, type=openapi.TYPE_INTEGER),
            openapi.Parameter('status', openapi.IN_QUERY, type=openapi.TYPE_STRING, enum=['running', 'completed', 'error', 'paused']),
        ],
        responses={200: "List of session summaries"}
    )
    def get(self, request, collection_id):
        from collection.models import Collection
        collection = get_object_or_404(Collection, id=collection_id, project__user=request.user)
        
        limit = int(request.query_params.get('limit', 50))
        status_filter = request.query_params.get('status')
        
        queryset = AgentMission.objects.filter(
            collection=collection
        ).select_related('collection', 'collection__project')
        
        if status_filter:
            queryset = queryset.filter(status=status_filter)
        
        sessions = queryset.order_by('-created_at')[:limit]
        serializer = SessionHistorySerializer(sessions, many=True)
        
        return Response({
            'collection_id': str(collection.id),
            'collection_name': collection.name,
            'total_sessions': queryset.count(),
            'sessions': serializer.data,
        })


class SessionComparisonView(APIView):
    """
    Compares two sessions side-by-side for regression analysis.
    
    GET /test-cases/sessions/compare/?baseline=<batch_id>&current=<batch_id>
    
    Returns a diff-style report showing:
    - Regressions (passed → failed)
    - Improvements (failed → passed)
    - Unchanged results
    - New steps not in baseline
    """
    permission_classes = [permissions.IsAuthenticated]

    @swagger_auto_schema(
        operation_description="Compare two sessions for regression analysis",
        manual_parameters=[
            openapi.Parameter('baseline', openapi.IN_QUERY, type=openapi.TYPE_STRING, required=True),
            openapi.Parameter('current', openapi.IN_QUERY, type=openapi.TYPE_STRING, required=True),
        ],
        responses={200: "Comparison Report"}
    )
    def get(self, request):
        baseline_id = request.query_params.get('baseline')
        current_id = request.query_params.get('current')
        
        if not baseline_id or not current_id:
            return Response(
                {'error': 'Both baseline and current batch_id query params are required.'},
                status=400
            )
        
        try:
            baseline = AgentMission.objects.get(batch_id=baseline_id, user=request.user)
            current = AgentMission.objects.get(batch_id=current_id, user=request.user)
        except AgentMission.DoesNotExist:
            return Response({'error': 'Session not found.'}, status=404)
        
        # Build step-level comparison by action_type
        baseline_steps = {
            s.action_type: s
            for s in baseline.steps.all()
        }
        current_steps = {
            s.action_type: s
            for s in current.steps.all()
        }
        
        all_action_types = set(baseline_steps.keys()) | set(current_steps.keys())
        
        regressions = []
        improvements = []
        unchanged = []
        new_in_current = []
        removed_from_baseline = []
        
        for action_type in sorted(all_action_types):
            b = baseline_steps.get(action_type)
            c = current_steps.get(action_type)
            
            if b and c:
                if b.status == 'passed' and c.status == 'failed':
                    regressions.append({
                        'action_type': action_type,
                        'baseline_status': b.status,
                        'current_status': c.status,
                        'baseline_thought': b.thought,
                        'current_thought': c.thought,
                    })
                elif b.status == 'failed' and c.status == 'passed':
                    improvements.append({
                        'action_type': action_type,
                        'baseline_status': b.status,
                        'current_status': c.status,
                        'baseline_thought': b.thought,
                        'current_thought': c.thought,
                    })
                else:
                    unchanged.append({
                        'action_type': action_type,
                        'baseline_status': b.status,
                        'current_status': c.status,
                    })
            elif c and not b:
                new_in_current.append({
                    'action_type': action_type,
                    'current_status': c.status,
                    'current_thought': c.thought,
                })
            elif b and not c:
                removed_from_baseline.append({
                    'action_type': action_type,
                    'baseline_status': b.status,
                    'baseline_thought': b.thought,
                })
        
        return Response({
            'baseline': {
                'batch_id': str(baseline.batch_id),
                'status': baseline.status,
                'pass_rate': baseline.pass_rate,
                'total_steps': baseline.total_steps,
                'completed_at': baseline.completed_at,
                'mission_type': baseline.mission_type,
            },
            'current': {
                'batch_id': str(current.batch_id),
                'status': current.status,
                'pass_rate': current.pass_rate,
                'total_steps': current.total_steps,
                'completed_at': current.completed_at,
                'mission_type': current.mission_type,
            },
            'summary': {
                'regressions': len(regressions),
                'improvements': len(improvements),
                'unchanged': len(unchanged),
                'new_steps': len(new_in_current),
                'removed_steps': len(removed_from_baseline),
                'regression_free': len(regressions) == 0,
            },
            'regressions': regressions,
            'improvements': improvements,
            'unchanged': unchanged,
            'new_steps': new_in_current,
            'removed_steps': removed_from_baseline,
        })


class DashboardSummaryView(APIView):
    """
    Returns a data-driven dashboard summary for the authenticated user.
    Includes: real pass rates, recent runs, recent sessions, project stats.
    """
    permission_classes = [permissions.IsAuthenticated]

    @swagger_auto_schema(
        operation_description="Get dashboard summary with real stats and recent activity",
        responses={200: "Dashboard JSON"}
    )
    @method_decorator(cache_page(30))  # Cache 30s
    def get(self, request):
        from django.db.models import Count, Q
        from django.utils import timezone as tz
        from datetime import timedelta

        user = request.user

        # Global stats
        all_runs = TestRun.objects.filter(test_case__endpoint__collection__project__user=user)
        all_missions = AgentMission.objects.filter(user=user)
        total_runs = all_runs.count()
        total_passed = all_runs.filter(status='passed').count()
        total_failed = all_runs.filter(status__in=['failed', 'error']).count()
        pass_rate = round((total_passed / total_runs) * 100, 1) if total_runs > 0 else 0

        # Recent runs (last 10)
        recent_runs = all_runs.select_related(
            'test_case', 'test_case__endpoint'
        ).order_by('-executed_at')[:10]
        recent_runs_data = [{
            'id': r.id,
            'test_case_name': r.test_case.name if r.test_case else 'Unknown',
            'endpoint_method': r.test_case.endpoint.method if r.test_case and r.test_case.endpoint else '',
            'endpoint_url': r.test_case.endpoint.url if r.test_case and r.test_case.endpoint else '',
            'status': r.status,
            'response_status': r.response_status,
            'response_time_ms': r.response_time_ms,
            'triggered_by': r.triggered_by,
            'executed_at': r.executed_at.isoformat() if r.executed_at else None,
            'batch_id': str(r.batch_id) if r.batch_id else None,
        } for r in recent_runs]

        # Recent sessions/missions (last 5)
        recent_sessions = all_missions.select_related(
            'collection', 'collection__project'
        ).order_by('-created_at')[:5]
        recent_sessions_data = [{
            'id': s.id,
            'batch_id': str(s.batch_id),
            'collection_name': s.collection.name if s.collection else 'Unknown',
            'project_name': s.collection.project.name if s.collection and s.collection.project else '',
            'status': s.status,
            'mission_type': s.mission_type,
            'total_steps': s.total_steps,
            'passed_steps': s.passed_steps,
            'failed_steps': s.failed_steps,
            'pass_rate': s.pass_rate,
            'completed_at': s.completed_at.isoformat() if s.completed_at else None,
            'created_at': s.created_at.isoformat() if s.created_at else None,
        } for s in recent_sessions]

        # Projects with stats
        from projects.models import Project
        projects = Project.objects.filter(user=user).annotate(
            run_count=Count('collections__endpoints__test_cases__runs')
        ).order_by('-run_count')[:5]
        projects_data = [{
            'id': str(p.id),
            'name': p.name,
            'total_runs': p.run_count,
        } for p in projects]

        # Activity feed (last 24h of runs)
        since = tz.now() - timedelta(hours=24)
        recent_24h = all_runs.filter(executed_at__gte=since)
        passed_24h = recent_24h.filter(status='passed').count()
        failed_24h = recent_24h.filter(status__in=['failed', 'error']).count()
        total_24h = recent_24h.count()

        return Response({
            'stats': {
                'total_runs': total_runs,
                'passed_runs': total_passed,
                'failed_runs': total_failed,
                'pass_rate': pass_rate,
                'total_sessions': all_missions.count(),
                'runs_last_24h': total_24h,
                'passed_last_24h': passed_24h,
                'failed_last_24h': failed_24h,
            },
            'recent_runs': recent_runs_data,
            'recent_sessions': recent_sessions_data,
            'projects': projects_data,
        })


class BatchReportView(APIView):
    """
    Returns an aggregated report for all runs in a batch (webhook/CI trigger).
    """
    permission_classes = [permissions.IsAuthenticated]

    @swagger_auto_schema(
        operation_description="Get aggregated batch report for webhook/CI runs",
        responses={200: "Batch Report JSON"}
    )
    def get(self, request, batch_id):
        runs = TestRun.objects.filter(
            batch_id=batch_id,
            test_case__endpoint__collection__project__user=request.user
        ).select_related('test_case', 'test_case__endpoint')

        if not runs.exists():
            return Response({'error': 'Batch not found or no runs completed.'}, status=404)

        total = runs.count()
        passed = runs.filter(status='passed').count()
        failed = runs.filter(status__in=['failed', 'error']).count()
        avg_time = runs.aggregate(Avg('response_time_ms'))['response_time_ms__avg'] or 0

        # Determine if this was an AI agent batch
        first_run = runs.first()
        is_agent = first_run.triggered_by == 'ai_agent'

        # Group by test case
        from collections import defaultdict
        test_groups = defaultdict(list)
        for run in runs:
            test_groups[run.test_case_id].append({
                'id': run.id,
                'status': run.status,
                'response_status': run.response_status,
                'response_time_ms': run.response_time_ms,
                'error_message': run.error_message,
                'executed_at': run.executed_at.isoformat() if run.executed_at else None,
            })

        test_summaries = []
        for tc_id, tc_runs in test_groups.items():
            tc = tc_runs[0]
            test_case_obj = first_run.test_case if first_run else None
            test_name = 'Unknown'
            endpoint_info = ''
            if tc_id and first_run and first_run.test_case:
                test_name = first_run.test_case.name or f'Test #{tc_id}'
                if first_run.test_case.endpoint:
                    endpoint_info = f"{first_run.test_case.endpoint.method} {first_run.test_case.endpoint.url}"

            tc_passed = sum(1 for r in tc_runs if r['status'] == 'passed')
            tc_failed = len(tc_runs) - tc_passed
            test_summaries.append({
                'test_case_id': tc_id,
                'test_name': test_name,
                'endpoint': endpoint_info,
                'runs': tc_runs,
                'passed': tc_passed,
                'failed': tc_failed,
                'latest_status': tc_runs[0]['status'],
                'flaky': False,
                'failure_categories': {},
                'failures': [],
            })

        # QA-standard enrichment: flake detection + root-cause classification
        pass_rate = round((passed / total) * 100, 1) if total > 0 else 0
        analysis_by_id = {
            a['test_case_id']: a for a in analyze_runs(runs)
        }
        for summary in test_summaries:
            info = analysis_by_id.get(summary['test_case_id'])
            if info:
                summary['flaky'] = info['flaky']
                summary['failure_categories'] = info['failure_breakdown']
                summary['failures'] = info['failures']
        flaky_tests = [
            {'test_case_id': s['test_case_id'], 'test_name': s['test_name']}
            for s in test_summaries if s['flaky']
        ]

        return Response({
            'batch_id': str(batch_id),
            'is_agent_batch': is_agent,
            'triggered_by': first_run.triggered_by if first_run else 'unknown',
            'summary': {
                'total_runs': total,
                'passed': passed,
                'failed': failed,
                'pass_rate': pass_rate,
                'avg_response_time_ms': round(avg_time, 1),
            },
            'qa': {
                'qa_verdict': qa_verdict(pass_rate, len(flaky_tests)),
                'flaky_tests': flaky_tests,
                'flaky_count': len(flaky_tests),
                'failure_breakdown': failure_breakdown(runs),
            },
            'tests': test_summaries,
            'executed_at': first_run.executed_at.isoformat() if first_run and first_run.executed_at else None,
        })


class AgentTakeoverView(APIView):
    """
    Allows a user to take over control of the agent's browser via VNC,
    or hand control back to the AI after manual intervention (e.g. CAPTCHA).
    
    POST /test-cases/missions/<batch_id>/takeover/
    { "action": "pause" }  — Agent pauses, user solves CAPTCHA via VNC
    { "action": "resume" } — Agent resumes from where it left off
    """
    permission_classes = [permissions.IsAuthenticated]

    @swagger_auto_schema(
        operation_description="Pause or resume the agent for human takeover",
        request_body=openapi.Schema(
            type=openapi.TYPE_OBJECT,
            required=['action'],
            properties={
                'action': openapi.Schema(type=openapi.TYPE_STRING, enum=['pause', 'resume', 'stop']),
                'note': openapi.Schema(type=openapi.TYPE_STRING, description='Optional note for the agent about what was done manually'),
            }
        ),
        responses={200: "Status updated"}
    )
    def post(self, request, batch_id):
        mission = get_object_or_404(AgentMission, batch_id=batch_id, user=request.user)
        action = request.data.get('action')
        note = request.data.get('note', '')

        if action == 'stop':
            if mission.status not in ('running', 'paused', 'pending'):
                return Response({"error": f"Mission is already {mission.status}."}, status=400)
            mission.status = 'error'
            mission.error_message = f"Stopped by user.{f' {note}' if note else ''}"
            mission.completed_at = timezone.now()
            mission.save()
            logger.info(f"[Agent] Mission {batch_id} stopped by user.")
            return Response({"status": "error", "message": "Mission stopped."})

        if mission.status not in ('running', 'paused'):
            return Response({"error": f"Mission is {mission.status}, cannot take over."}, status=400)

        if action == 'pause':
            if mission.status != 'running':
                return Response({"error": 'Mission is not running.'}, status=400)
            mission.status = 'paused'
            mission.save()
            # Inject a guidance prompt so the agent knows it was paused
            AgentPrompt.objects.create(
                mission=mission,
                prompt=f"[HUMAN TAKEOVER] The user has paused you to manually intervene. "
                       f"When you receive the resume signal, continue from where you left off."
                       f"{f' Manual intervention note: {note}' if note else ''}"
            )
            logger.info(f"[Agent] Mission {batch_id} paused for human takeover.")
            return Response({"status": "paused", "message": "Agent paused. Solve the CAPTCHA or blocker via VNC, then resume."})

        elif action == 'resume':
            if mission.status != 'paused':
                return Response({"error": 'Mission is not paused.'}, status=400)
            mission.status = 'running'
            mission.save()
            AgentPrompt.objects.create(
                mission=mission,
                prompt=f"[HUMAN TAKEOVER COMPLETE] The user has finished manual intervention and resumed you. "
                       f"Continue the mission."
                       f"{f' What was done: {note}' if note else ''}"
            )
            logger.info(f"[Agent] Mission {batch_id} resumed after human takeover.")
            return Response({"status": "running", "message": "Agent resumed."})

        return Response({"error": "Invalid action. Use 'pause', 'resume', or 'stop'."}, status=400)


class DatasetExportView(APIView):
    """
    INTERNAL: Exports training data for QA model fine-tuning.
    Admin-only. Produces three dataset types:
      1. agent_decisions  — Agent thought → action → outcome traces
      2. test_generation  — Endpoint schema → generated test → pass/fail
      3. api_interactions — Request/response patterns across real APIs
    
    GET /test-cases/datasets/export/?type=agent_decisions&limit=1000
    """
    permission_classes = [permissions.IsAdminUser]

    @swagger_auto_schema(
        operation_description="Export training dataset for QA model fine-tuning (admin only)",
        manual_parameters=[
            openapi.Parameter('type', openapi.IN_QUERY, type=openapi.TYPE_STRING, required=True,
                enum=['agent_decisions', 'test_generation', 'api_interactions']),
            openapi.Parameter('limit', openapi.IN_QUERY, type=openapi.TYPE_INTEGER),
            openapi.Parameter('since', openapi.IN_QUERY, type=openapi.TYPE_STRING,
                description='ISO date — only include data after this date'),
        ],
        responses={200: "Training dataset JSONL"}
    )
    def get(self, request):
        from django.http import HttpResponse
        from django.utils import timezone as tz
        from datetime import timedelta
        import json

        dataset_type = request.query_params.get('type')
        limit = int(request.query_params.get('limit', 2000))
        since_str = request.query_params.get('since')

        since = None
        if since_str:
            try:
                since = tz.datetime.fromisoformat(since_str.replace('Z', '+00:00'))
            except Exception:
                since = tz.now() - timedelta(days=90)
        else:
            since = tz.now() - timedelta(days=90)  # Default: last 90 days

        if dataset_type == 'agent_decisions':
            records = self._build_agent_decisions(since, limit)
        elif dataset_type == 'test_generation':
            records = self._build_test_generation(since, limit)
        elif dataset_type == 'api_interactions':
            records = self._build_api_interactions(since, limit)
        else:
            return Response({
                'error': 'Invalid type. Use: agent_decisions, test_generation, api_interactions',
            }, status=400)

        # Output as JSONL (one JSON object per line — standard for training pipelines)
        jsonl = '\n'.join(json.dumps(r, default=str) for r in records)
        response = HttpResponse(jsonl, content_type='application/jsonl')
        response['Content-Disposition'] = f'attachment; filename="qa_{dataset_type}_dataset.jsonl"'
        response['X-Dataset-Count'] = str(len(records))
        return response

    def _build_agent_decisions(self, since, limit):
        """
        Agent Decision Traces — for fine-tuning the autonomous agent.
        Format: { instruction, context, action_chosen, outcome }
        """
        steps = AgentMissionStep.objects.filter(
            created_at__gte=since,
            mission__status__in=['completed', 'error']
        ).select_related('mission', 'mission__collection').order_by('mission_id', 'step_number')[:limit * 3]

        records = []
        for step in steps:
            # Skip FINISH steps and steps without thoughts
            if step.action_type == 'FINISH' or not step.thought:
                continue

            mission = step.mission
            records.append({
                'instruction': f"You are a QA agent testing '{mission.collection.name}'. User story: {mission.user_story}",
                'context': {
                    'scenarios': mission.scenarios,
                    'categories': mission.categories,
                    'mission_type': mission.mission_type,
                    'step_number': step.step_number,
                    'previous_actions': list(
                        mission.steps.filter(step_number__lt=step.step_number)
                        .values_list('action_type', flat=True)
                    ),
                },
                'action_chosen': {
                    'type': step.action_type,
                    'details': step.details,
                    'thought': step.thought,
                },
                'outcome': {
                    'status': step.status,
                    'response_status': step.response_status,
                    'response_preview': (step.response_body or '')[:500],
                },
                # Training labels
                'quality_score': 1.0 if step.status == 'passed' else 0.0,
            })

            if len(records) >= limit:
                break

        return records

    def _build_test_generation(self, since, limit):
        """
        Test Generation Patterns — for fine-tuning the test generator.
        Format: { endpoint_schema, scenario, generated_test, passed }
        """
        runs = TestRun.objects.filter(
            executed_at__gte=since,
            test_case__ai_generated=True,
            triggered_by__in=['ai', 'ai_agent', 'manual']
        ).select_related(
            'test_case', 'test_case__endpoint'
        ).order_by('-executed_at')[:limit]

        records = []
        for run in runs:
            tc = run.test_case
            ep = tc.endpoint
            if not ep:
                continue

            records.append({
                'endpoint_schema': {
                    'method': ep.method,
                    'url': ep.url,
                    'name': ep.name,
                    'description': ep.description,
                    'request_body': ep.request_body,
                    'auth_type': ep.auth_type,
                },
                'scenario': {
                    'category': tc.category,
                    'priority': tc.priority,
                    'user_story': tc.user_story,
                    'tags': tc.tags,
                },
                'generated_test': {
                    'name': tc.name,
                    'description': tc.description,
                    'headers': tc.headers,
                    'body': tc.body,
                    'query_params': tc.query_params,
                    'expected_status': tc.expected_status,
                    'assertions': tc.assertions,
                    'test_script': tc.test_script,
                },
                'outcome': {
                    'passed': run.status == 'passed',
                    'actual_status': run.response_status,
                    'error': run.error_message,
                },
                'quality_score': 1.0 if run.status == 'passed' else 0.0,
            })

        return records

    def _build_api_interactions(self, since, limit):
        """
        API Interaction Patterns — for training general API understanding.
        Format: { method, url, request, response, context }
        Anonymizes user-specific data (tokens, emails, IDs become placeholders).
        """
        runs = TestRun.objects.filter(
            executed_at__gte=since,
            response_status__isnull=False
        ).select_related(
            'test_case', 'test_case__endpoint'
        ).order_by('-executed_at')[:limit]

        records = []
        for run in runs:
            tc = run.test_case
            ep = tc.endpoint if tc else None

            # Anonymize request body — replace real values with type hints
            body = self._anonymize_payload(tc.body) if tc.body else {}
            headers = self._anonymize_headers(tc.headers) if tc.headers else {}

            records.append({
                'method': ep.method if ep else 'UNKNOWN',
                'url_pattern': ep.url if ep else '/',
                'request': {
                    'headers': headers,
                    'body': body,
                    'query_params': tc.query_params,
                },
                'response': {
                    'status': run.response_status,
                    'body_preview': self._truncate_response(run.response_body),
                    'time_ms': run.response_time_ms,
                },
                'context': {
                    'category': tc.category,
                    'runner_type': tc.runner_type,
                    'was_ai_generated': tc.ai_generated,
                },
                'quality_score': 1.0 if run.status == 'passed' else 0.0,
            })

        return records

    def _anonymize_payload(self, data):
        """Replace real values with type annotations for safe training data."""
        if not isinstance(data, dict):
            return data
        anonymized = {}
        for key, value in data.items():
            if isinstance(value, str):
                lower = value.lower()
                if 'email' in key.lower() or '@' in str(value):
                    anonymized[key] = '<EMAIL>'
                elif 'password' in key.lower() or 'token' in key.lower() or 'secret' in key.lower():
                    anonymized[key] = '<REDACTED>'
                elif 'name' in key.lower():
                    anonymized[key] = '<NAME>'
                elif value.isdigit():
                    anonymized[key] = '<INTEGER>'
                elif value.startswith('http'):
                    anonymized[key] = '<URL>'
                else:
                    anonymized[key] = f'<STRING:{len(value)}>'
            elif isinstance(value, (int, float)):
                anonymized[key] = f'<{type(value).__name__.upper()}>'
            elif isinstance(value, bool):
                anonymized[key] = value  # Keep booleans
            elif isinstance(value, dict):
                anonymized[key] = self._anonymize_payload(value)
            elif isinstance(value, list):
                anonymized[key] = [self._anonymize_payload(item) if isinstance(item, dict) else item for item in value]
            else:
                anonymized[key] = value
        return anonymized

    def _anonymize_headers(self, headers):
        """Redact auth headers, keep structural headers."""
        if not isinstance(headers, dict):
            return headers
        safe = {}
        for key, value in headers.items():
            lower = key.lower()
            if any(s in lower for s in ['auth', 'token', 'key', 'cookie', 'session']):
                safe[key] = '<REDACTED>'
            else:
                safe[key] = value
        return safe

    def _truncate_response(self, body, max_len=500):
        """Truncate response body for dataset compactness."""
        if not body:
            return ''
        text = str(body)
        return text[:max_len] + '...' if len(text) > max_len else text


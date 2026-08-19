from rest_framework import generics, permissions, status
from rest_framework.response import Response
from rest_framework.views import APIView
from django.shortcuts import get_object_or_404
from django.utils.decorators import method_decorator
from django.views.decorators.cache import cache_page
from django_ratelimit.decorators import ratelimit
from collection.models import Endpoint
from projects.models import Project
from .models import TestCase, TestRun, AgentMission, AgentMissionStep, AgentPrompt
from .serializers import (
    TestCaseSerializer, TestRunSerializer, 
    AgentMissionSerializer, AgentMissionStepSerializer, AgentPromptSerializer
)
from .ai_generator import AITestGenerator
from .runner_service import RunnerService
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

        # Get the latest run for each test case
        from django.db.models import OuterRef, Subquery
        latest_run_id = TestRun.objects.filter(
            test_case=OuterRef('pk')
        ).order_by('-executed_at').values('id')[:1]

        latest_runs = TestRun.objects.filter(id__in=Subquery(latest_run_id))
        
        passed_count = latest_runs.filter(status="passed").count()
        failed_count = latest_runs.filter(status__in=["failed", "error"]).count()
        
        # Group by Endpoint
        from collection.models import Endpoint
        endpoint_status = []
        endpoints = Endpoint.objects.filter(collection__project=project)
        
        for endpoint in endpoints:
            endpoint_tests = test_cases.filter(endpoint=endpoint)
            
            tests_list = []
            endpoint_failed = 0
            has_runs = False
            
            for test in endpoint_tests:
                # Get the latest run for this specific test from our pre-fetched latest_runs
                latest_run = latest_runs.filter(test_case=test).first()
                
                run_status = "no_runs"
                if latest_run:
                    run_status = latest_run.status
                    has_runs = True
                    if run_status in ["failed", "error"]:
                        endpoint_failed += 1
                
                tests_list.append({
                    "id": test.id,
                    "name": test.name,
                    "status": run_status,
                    "last_run_id": latest_run.id if latest_run else None,
                    "last_run_at": latest_run.executed_at if latest_run else None,
                    "category": test.category,
                    "priority": test.priority
                })

            endpoint_status.append({
                "id": endpoint.id,
                "name": endpoint.name,
                "method": endpoint.method,
                "url": endpoint.url,
                "collection_id": endpoint.collection.id,
                "collection_name": endpoint.collection.name,
                "total_tests": endpoint_tests.count(),
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
            is_safe_mode=is_safe_mode
        )

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
            status="running" # Task will take over
        )

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
                "summary": {"total": 0, "passed": 0, "failed": 0, "pass_rate": 0},
                "endpoints": []
            })
        
        # Get the latest run for each test case
        from django.db.models import OuterRef, Subquery
        latest_run_id = TestRun.objects.filter(
            test_case=OuterRef('pk')
        ).order_by('-executed_at').values('id')[:1]

        latest_runs = TestRun.objects.filter(id__in=Subquery(latest_run_id))
        
        passed_count = latest_runs.filter(status="passed").count()
        failed_count = latest_runs.filter(status__in=["failed", "error"]).count()
        
        # Group by Endpoint
        endpoint_status = []
        
        for endpoint in endpoints:
            endpoint_tests = test_cases.filter(endpoint=endpoint)
            
            tests_list = []
            endpoint_failed = 0
            has_runs = False
            
            for test in endpoint_tests:
                latest_run = latest_runs.filter(test_case=test).first()
                
                run_status = "no_runs"
                if latest_run:
                    run_status = latest_run.status
                    has_runs = True
                    if run_status in ["failed", "error"]:
                        endpoint_failed += 1
                
                tests_list.append({
                    "id": test.id,
                    "name": test.name,
                    "status": run_status,
                    "last_run_id": latest_run.id if latest_run else None,
                    "last_run_at": latest_run.executed_at if latest_run else None,
                    "category": test.category,
                    "priority": test.priority
                })

            endpoint_status.append({
                "id": endpoint.id,
                "name": endpoint.name,
                "method": endpoint.method,
                "url": endpoint.url,
                "total_tests": endpoint_tests.count(),
                "failed_tests": endpoint_failed,
                "status": "failing" if endpoint_failed > 0 else "passing" if has_runs else "no_runs",
                "tests": tests_list
            })

        return Response({
            "collection_name": collection.name,
            "collection_id": str(collection.id),
            "project_name": collection.project.name,
            "project_id": str(collection.project.id),
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

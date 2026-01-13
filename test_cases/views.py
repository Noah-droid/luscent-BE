from rest_framework import generics, permissions, status
from rest_framework.response import Response
from rest_framework.views import APIView
from django.shortcuts import get_object_or_404
from django.utils.decorators import method_decorator
from django_ratelimit.decorators import ratelimit
from collection.models import Collection
from projects.models import Project
from .models import TestCase, TestRun
from .serializers import TestCaseSerializer, TestRunSerializer
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
        collection_id = request.data.get("collection")
        if not collection_id:
            return Response({"error": "collection_id required"}, status=400)

        collection_item = get_object_or_404(Collection, id=collection_id, project__user=request.user)
        
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
        if not deduct_tokens(request.user, COST, f"AI Generation: {collection_item.name}"):
             return Response(
                 {"error": f"Insufficient tokens. Required: {COST}, Balance: {request.user.token_balance}"}, 
                 status=402 # Payment Required
             )

        generator = AITestGenerator()
        try:
            # Call AI
            draft_tests = generator.generate_tests(
                collection_item, 
                runner_type=runner_type,
                category=category,
                layer=layer,
                scenarios=scenarios,
                user_story=user_story, 
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
        # Expecting structure: { "collection_id": 1, "auto_run": true, "tests": [...] }
        payload = request.data
        
        # 1. Parse Root Params
        collection_id = payload.get("collection_id") or request.data.get("collection")
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
        
        # Resolve Collection Context globally if provided
        global_collection = None
        if collection_id:
            try:
                global_collection = get_object_or_404(Collection, id=collection_id, project__user=request.user)
            except:
                pass 
        
        # Save user_story if present in test data
        for index, data in enumerate(test_data_list):
            try:
                # 2. Resolve Collection (Item level overrides global)
                collection = global_collection
                c_id = data.get("collection") or data.get("collection_id")
                
                if c_id:
                     # If item specifies different collection, look it up
                     try:
                         collection = Collection.objects.get(id=c_id, project__user=request.user)
                     except Collection.DoesNotExist:
                         errors.append({"index": index, "error": f"Collection {c_id} not found"})
                         continue
                
                if not collection:
                     errors.append({"index": index, "error": "Missing collection_id (global or item level)"})
                     continue


                test_case = TestCase.objects.create(
                    collection=collection,
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
                            run_test_case_task.delay(test.id, batch_id=batch_id, triggered_by="manual")
                        except Exception as e:
                            logger.error(f"Failed to queue task: {e}. Falling back to sync.")
                            RunnerService().execute_test(test.id, batch_id=batch_id, triggered_by="manual")
                    else:
                        RunnerService().execute_test(test.id, batch_id=batch_id, triggered_by="manual")
            else:
                errors.append({"error": f"Insufficient tokens for auto-run. Required: {total_auto_run_cost}, Balance: {request.user.token_balance}. Tests were created but not run."})


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
             return Response(
                 {"error": f"Insufficient tokens. Required: {cost}, Balance: {request.user.token_balance}"}, 
                 status=402
             )
        
        if run_async:
            try:
                task = run_test_case_task.delay(test_case.id)
                return Response({"message": "Test queued", "task_id": task.id}, status=status.HTTP_202_ACCEPTED)
            except Exception as e:
                logger.error(f"Celery error: {e}. Falling back to sync run.")
        
        runner = RunnerService()
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
            return Response({
                'error': f'Insufficient tokens. Required: {total_cost}, Balance: {request.user.token_balance}'
            }, status=status.HTTP_402_PAYMENT_REQUIRED)

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
                    triggered_by="webhook"
                )
                run_ids.append(task.id)
                
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
        batch_id = self.request.query_params.get("batch_id")
        
        queryset = TestRun.objects.filter(test_case__collection__project__user=self.request.user)
        
        if test_case_id:
            queryset = queryset.filter(test_case__id=test_case_id)
        
        if batch_id:
            queryset = queryset.filter(batch_id=batch_id)
            
        return queryset.order_by('-executed_at')

class TestRunDetailView(generics.RetrieveAPIView):
    serializer_class = TestRunSerializer
    permission_classes = [permissions.IsAuthenticated]
    
    def get_queryset(self):
        return TestRun.objects.filter(test_case__collection__project__user=self.request.user)


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
    def get(self, request, project_id):
        project = get_object_or_404(Project, id=project_id, user=request.user)
        
        # Get all test cases for this project
        test_cases = TestCase.objects.filter(collection__project=project)
        
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
        
        # Group by Collection (Endpoint)
        endpoint_status = []
        collections = Collection.objects.filter(project=project)
        
        for coll in collections:
            coll_tests = test_cases.filter(collection=coll)
            
            tests_list = []
            coll_failed = 0
            has_runs = False
            
            for test in coll_tests:
                # Get the latest run for this specific test from our pre-fetched latest_runs
                latest_run = latest_runs.filter(test_case=test).first()
                
                run_status = "no_runs"
                if latest_run:
                    run_status = latest_run.status
                    has_runs = True
                    if run_status in ["failed", "error"]:
                        coll_failed += 1
                
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
                "id": coll.id,
                "name": coll.name,
                "method": coll.method,
                "url": coll.url,
                "total_tests": coll_tests.count(),
                "failed_tests": coll_failed,
                "status": "failing" if coll_failed > 0 else "passing" if has_runs else "no_runs",
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
                'user_story': openapi.Schema(type=openapi.TYPE_STRING, description="Optional: Context or requirements that apply to all endpoints in this batch")
            }
        ),
        responses={202: "Auto-Pilot started"}
    )
    def post(self, request, project_id):
        project = get_object_or_404(Project, id=project_id, user=request.user)
        scenarios = request.data.get("scenarios", ["HAPPY_PATH", "VALIDATION_ERROR", "SECURITY"])
        user_story = request.data.get("user_story", "")
        
        if not HAS_CELERY:
            return Response({"error": "Auto-Pilot requires Celery for background processing."}, status=501)

        import uuid
        batch_id = uuid.uuid4()
        
   
        from .tasks import project_auto_pilot_task
        
        project_auto_pilot_task.delay(
            project_id=str(project.id),
            user_id=request.user.id,
            scenarios=scenarios,
            batch_id=str(batch_id),
            user_story=user_story
        )

        return Response({
            "message": "Auto-Pilot started successfully",
            "batch_id": batch_id,
            "description": f"Generating and running tests for all endpoints in {project.name}."
        }, status=status.HTTP_202_ACCEPTED)

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


class TestCaseListCreateView(generics.ListCreateAPIView):
    # Kept for backward compatibility and manual creation
    serializer_class = TestCaseSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        return TestCase.objects.filter(collection__project__user=self.request.user)

    def perform_create(self, serializer):
        # Ensure we don't allow creating specific IDs if not needed
        serializer.save()



class DraftTestPlanView(APIView):
    """
    Step 1 of Hybrid Flow: Generate a JSON Draft Plan via AI.
    Does NOT save to DB.
    Input: { "collection_id": 1, "scenarios": ["HAPPY_PATH", "VALIDATION"], ... }
    Output: JSON List of proposed tests.
    """
    permission_classes = [permissions.IsAuthenticated]

    @method_decorator(ratelimit(key='user', rate='2/h', method='POST'))
    def post(self, request):
        collection_id = request.data.get("collection")
        if not collection_id:
            return Response({"error": "collection_id required"}, status=400)

        collection_item = get_object_or_404(Collection, id=collection_id, project__user=request.user)
        
        runner_type = request.data.get("runner_type", "http")
        category = request.data.get("category", "functional")
        layer = request.data.get("layer", "backend")
        scenarios = request.data.get("scenarios", []) # e.g. ["HAPPY_PATH"]

        # Enforce logic: Performance = Load Runner
        if category.lower() == "performance":
            runner_type = "load"

        generator = AITestGenerator()
        try:
            # 1. Call AI
            draft_tests = generator.generate_tests(
                collection_item, 
                runner_type=runner_type,
                category=category,
                layer=layer,
                scenarios=scenarios
            )
            return Response(draft_tests, status=status.HTTP_200_OK)
        except Exception as e:
            return Response({"error": f"Draft generation failed: {str(e)}"}, status=500)



class BatchCreateTestsView(APIView):
    """
    Step 2 of Hybrid Flow: Save approved/edited drafts to DB.
    Input: [ { "name": "Test 1", "collection_id": 1, ... }, ... ]
    Output: [ { "id": 55, ... } ]
    """
    permission_classes = [permissions.IsAuthenticated]

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
                    ai_generated=data.get("ai_generated", True) # Default to true since this flow is "Batch AI"
                )
                created_tests.append(test_case)
            except Exception as e:
                 errors.append({"index": index, "error": str(e)})

        # Handle Auto-Run
        if should_auto_run and created_tests:
            for test in created_tests:
                if HAS_CELERY:
                    try:
                        run_test_case_task.delay(test.id)
                    except Exception as e:
                        logger.error(f"Failed to queue task: {e}. Falling back to sync.")
                        RunnerService().execute_test(test.id)
                else:
                    RunnerService().execute_test(test.id)


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



class TestRunListView(generics.ListAPIView):
    serializer_class = TestRunSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        test_case_id = self.kwargs["test_case_id"]
        return TestRun.objects.filter(
            test_case__id=test_case_id,
            test_case__collection__project__user=self.request.user
        ).order_by('-executed_at')

class TestRunDetailView(generics.RetrieveAPIView):
    serializer_class = TestRunSerializer
    permission_classes = [permissions.IsAuthenticated]
    
    def get_queryset(self):
        return TestRun.objects.filter(test_case__collection__project__user=self.request.user)

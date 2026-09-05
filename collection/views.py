from datetime import timedelta

from rest_framework import permissions, status
from rest_framework.views import APIView
from rest_framework.response import Response
from django.db.models import Q
from django.shortcuts import get_object_or_404
from django.utils import timezone
from drf_yasg.utils import swagger_auto_schema
from drf_yasg import openapi
import traceback

from .models import Collection, Endpoint, ImportJob
from .serializers import CollectionSerializer, EndpointSerializer, ImportJobSerializer
from projects.models import Project
from .crawler import crawl_url
from .importer import queue_swagger_import

try:
    from .tasks import import_crawler_task
    HAS_CELERY = True
except Exception:
    HAS_CELERY = False


class CollectionListCreateView(APIView):
    """
    Handles listing all collections for a project or creating a new one.
    """
    permission_classes = [permissions.IsAuthenticated]

    @swagger_auto_schema(
        operation_description="List all collections for a project",
        responses={200: CollectionSerializer(many=True)}
    )
    def get(self, request, project_id):
        project = get_object_or_404(Project, id=project_id, user=request.user)
        collections = Collection.objects.filter(project=project)
        return Response(CollectionSerializer(collections, many=True).data)

    @swagger_auto_schema(
        operation_description="Create a new collection for a project",
        request_body=CollectionSerializer,
        responses={201: CollectionSerializer()}
    )
    def post(self, request, project_id):
        project = get_object_or_404(Project, id=project_id, user=request.user)
        serializer = CollectionSerializer(data=request.data)
        if serializer.is_valid():
            # Inherit base_url from parent project if not provided
            data = serializer.validated_data
            if not data.get('base_url') and project.target_url:
                data['base_url'] = project.target_url
            serializer.save(project=project)
            return Response(serializer.data, status=status.HTTP_201_CREATED)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


class CollectionDetailView(APIView):
    """
    Handles retrieving, updating, and deleting a specific collection.
    """
    permission_classes = [permissions.IsAuthenticated]

    @swagger_auto_schema(
        operation_description="Retrieve a specific collection",
        responses={200: CollectionSerializer()}
    )
    def get(self, request, pk):
        collection = get_object_or_404(Collection, id=pk, project__user=request.user)
        return Response(CollectionSerializer(collection).data)

    @swagger_auto_schema(
        operation_description="Update a collection",
        request_body=CollectionSerializer,
        responses={200: CollectionSerializer()}
    )
    def put(self, request, pk):
        collection = get_object_or_404(Collection, id=pk, project__user=request.user)
        serializer = CollectionSerializer(collection, data=request.data, partial=True)
        if serializer.is_valid():
            serializer.save()
            return Response(serializer.data)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    @swagger_auto_schema(
        operation_description="Delete a collection",
        responses={204: "Deleted"}
    )
    def delete(self, request, pk):
        collection = get_object_or_404(Collection, id=pk, project__user=request.user)
        collection.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class EndpointListCreateView(APIView):
    """
    Handles listing all endpoints in a collection or creating a manual one.
    """
    permission_classes = [permissions.IsAuthenticated]

    @swagger_auto_schema(
        operation_description="List all endpoints in a collection",
        responses={200: EndpointSerializer(many=True)}
    )
    def get(self, request, collection_id):
        collection = get_object_or_404(Collection, id=collection_id, project__user=request.user)
        endpoints = Endpoint.objects.filter(collection=collection)
        return Response(EndpointSerializer(endpoints, many=True).data)

    @swagger_auto_schema(
        operation_description="Create a manual endpoint in a collection",
        request_body=EndpointSerializer,
        responses={201: EndpointSerializer()}
    )
    def post(self, request, collection_id):
        collection = get_object_or_404(Collection, id=collection_id, project__user=request.user)
        serializer = EndpointSerializer(data=request.data)
        if serializer.is_valid():
            serializer.save(collection=collection)
            return Response(serializer.data, status=status.HTTP_201_CREATED)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


class EndpointDetailView(APIView):
    """
    Handles retrieving, updating, and deleting a specific endpoint.
    """
    permission_classes = [permissions.IsAuthenticated]

    @swagger_auto_schema(
        operation_description="Retrieve a specific endpoint",
        responses={200: EndpointSerializer()}
    )
    def get(self, request, pk):
        endpoint = get_object_or_404(Endpoint, id=pk, collection__project__user=request.user)
        return Response(EndpointSerializer(endpoint).data)

    @swagger_auto_schema(
        operation_description="Update an endpoint",
        request_body=EndpointSerializer,
        responses={200: EndpointSerializer()}
    )
    def put(self, request, pk):
        endpoint = get_object_or_404(Endpoint, id=pk, collection__project__user=request.user)
        serializer = EndpointSerializer(endpoint, data=request.data, partial=True)
        if serializer.is_valid():
            serializer.save()
            return Response(serializer.data)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    @swagger_auto_schema(
        operation_description="Delete an endpoint",
        responses={204: "Deleted"}
    )
    def delete(self, request, pk):
        endpoint = get_object_or_404(Endpoint, id=pk, collection__project__user=request.user)
        endpoint.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class ImportJobListView(APIView):
    """
    Recent import jobs across the user's collections.

    Always returns active jobs (queued/running) plus jobs finished in the last
    few minutes, newest first — enough for UIs to render live progress toasts
    without polling the broker. Optional ?collection=<uuid> narrows to one collection.
    """

    permission_classes = [permissions.IsAuthenticated]

    @swagger_auto_schema(
        operation_description="List recent/active import jobs",
        manual_parameters=[
            openapi.Parameter(
                "collection", openapi.IN_QUERY, type=openapi.TYPE_STRING, required=False,
                description="Filter to a single collection (UUID)",
            )
        ],
        responses={200: ImportJobSerializer(many=True)}
    )
    def get(self, request):
        qs = ImportJob.objects.filter(collection__project__user=request.user).select_related(
            "collection__project"
        )
        collection_id = request.query_params.get("collection")
        if collection_id:
            qs = qs.filter(collection_id=collection_id)

        recent_cutoff = timezone.now() - timedelta(minutes=3)
        qs = qs.filter(Q(status__in=["queued", "running"]) | Q(finished_at__gte=recent_cutoff))
        jobs = qs[:20]
        return Response(ImportJobSerializer(jobs, many=True).data)


class SwaggerImportView(APIView):
    """
    Import endpoints from a Swagger/OpenAPI URL or uploaded file into an EXISTING
    collection. Always runs through the background import queue (Celery when
    available, inline otherwise) so large specs never block the web request.
    Returns 202 with a job_id; progress is surfaced via GET /collections/import-jobs/.
    """

    permission_classes = [permissions.IsAuthenticated]

    @swagger_auto_schema(
        operation_description="Queue an import of endpoints from Swagger/OpenAPI into an EXISTING collection",
        request_body=openapi.Schema(
            type=openapi.TYPE_OBJECT,
            properties={
                'swagger_url': openapi.Schema(type=openapi.TYPE_STRING),
                'file': openapi.Schema(type=openapi.TYPE_FILE),
                'skip_validation': openapi.Schema(type=openapi.TYPE_BOOLEAN, default=False)
            }
        ),
        responses={202: "Accepted (queued)", 400: "Bad Request", 409: "Import already running"}
    )
    def post(self, request, collection_id):
        collection = get_object_or_404(Collection, id=collection_id, project__user=request.user)
        swagger_url = (request.data.get("swagger_url") or "").strip()
        swagger_file = request.FILES.get("file")

        if not swagger_url and not swagger_file:
            return Response({"error": "Provide swagger_url or upload a file."}, status=400)

        raw_skip = request.data.get("skip_validation", False)
        skip_validation = str(raw_skip).lower() in ('true', '1', 'yes')

        # Only one active import per collection at a time
        if ImportJob.objects.filter(collection=collection, status__in=["queued", "running"]).exists():
            return Response(
                {"error": "An import is already running for this collection."},
                status=status.HTTP_409_CONFLICT,
            )

        # --- Upload hardening ---
        MAX_SPEC_SIZE = 5 * 1024 * 1024  # 5 MB
        if swagger_file is not None:
            if swagger_file.size > MAX_SPEC_SIZE:
                return Response({"error": "File too large (max 5 MB)."}, status=400)
            name = (swagger_file.name or "").lower()
            if not name.endswith(('.json', '.yaml', '.yml')):
                return Response({"error": "Only .json, .yaml, and .yml files are accepted."}, status=400)
            try:
                raw_bytes = swagger_file.read()
                raw_text = raw_bytes.decode("utf-8")
            except UnicodeDecodeError:
                raw_text = raw_bytes.decode("utf-8", errors="replace")
            job = queue_swagger_import(
                collection,
                source="file",
                spec_name=swagger_file.name or "spec",
                spec_text=raw_text,
                skip_validation=skip_validation,
            )
        else:
            job = queue_swagger_import(
                collection,
                source="url",
                spec_name=swagger_url,
                skip_validation=skip_validation,
            )

        return Response(
            {
                "message": "Import queued — you can keep working while endpoints are added.",
                "job_id": job.id,
                "collection_id": collection.id,
            },
            status=status.HTTP_202_ACCEPTED,
        )


class CrawlerImportView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    @swagger_auto_schema(
        operation_description="Crawl a website and import pages as endpoints into an EXISTING collection",
        request_body=openapi.Schema(
            type=openapi.TYPE_OBJECT,
            required=['url'],
            properties={
                'url': openapi.Schema(type=openapi.TYPE_STRING),
                'max_pages': openapi.Schema(type=openapi.TYPE_INTEGER, default=50),
                'async': openapi.Schema(type=openapi.TYPE_BOOLEAN, default=True)
            }
        )
    )
    def post(self, request, collection_id):
        collection = get_object_or_404(Collection, id=collection_id, project__user=request.user)
        start_url = request.data.get("url")
        max_pages = int(request.data.get("max_pages", 50))
        run_async = request.data.get("async", True) and HAS_CELERY

        if not start_url: return Response({"error": "url is required"}, status=400)

        if run_async:
            task = import_crawler_task.delay(collection.id, start_url, max_pages=max_pages)
            return Response({"message": "Crawler started", "task_id": task.id}, status=202)

        try:
            endpoints = crawl_url(start_url, max_pages=max_pages)
            
            collection.source = "crawler"
            collection.base_url = start_url
            collection.save()
            
            for e in endpoints:
                Endpoint.objects.update_or_create(
                    collection=collection,
                    method=e["method"],
                    url=e["full_url"],
                    defaults={
                        "name": e["name"],
                        "description": e.get("description", "")
                    }
                )
            
            return Response({"message": f"Crawled and imported {len(endpoints)} pages"}, status=201)
        except Exception as e:
            return Response({"error": str(e)}, status=500)

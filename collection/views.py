from rest_framework import generics, permissions, status
from .models import Collection, Endpoint
from rest_framework.views import APIView
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from django.shortcuts import get_object_or_404
from .serializers import CollectionSerializer, EndpointSerializer
from projects.models import Project
from .openapi_parser import (
    fetch_spec_from_url, load_spec_from_text, validate_openapi, parse_paths_to_endpoints
)
from .crawler import crawl_url
import traceback
from drf_yasg.utils import swagger_auto_schema
from drf_yasg import openapi
from django.utils import timezone


try:
    from .tasks import import_swagger_task, import_crawler_task  
    HAS_CELERY = True
except Exception:
    HAS_CELERY = False


class CollectionListCreateView(generics.ListCreateAPIView):
    """
    List all collections for a project or create a new one.
    """
    serializer_class = CollectionSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        project_id = self.kwargs["project_id"]
        return Collection.objects.filter(project__id=project_id,
                                       project__user=self.request.user)

    def perform_create(self, serializer):
        project_id = self.kwargs["project_id"]
        project = get_object_or_404(Project, id=project_id, user=self.request.user)
        serializer.save(project=project)


class CollectionDetailView(generics.RetrieveUpdateDestroyAPIView):
    """
    Retrieve, update or delete a collection.
    """
    serializer_class = CollectionSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        return Collection.objects.filter(project__user=self.request.user)


class EndpointListCreateView(generics.ListCreateAPIView):
    """
    List all endpoints in a collection or create a manual one.
    """
    serializer_class = EndpointSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        collection_id = self.kwargs["collection_id"]
        return Endpoint.objects.filter(collection__id=collection_id,
                                     collection__project__user=self.request.user)

    def perform_create(self, serializer):
        collection_id = self.kwargs["collection_id"]
        collection = get_object_or_404(Collection, id=collection_id, collection__project__user=self.request.user)
        serializer.save(collection=collection)


class EndpointDetailView(generics.RetrieveUpdateDestroyAPIView):
    """
    Retrieve, update or delete an endpoint definition.
    """
    serializer_class = EndpointSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        return Endpoint.objects.filter(collection__project__user=self.request.user)


class SwaggerImportView(APIView):
    permission_classes = [IsAuthenticated]

    @swagger_auto_schema(
        operation_description="Import endpoints from Swagger/OpenAPI into a new collection",
        request_body=openapi.Schema(
            type=openapi.TYPE_OBJECT,
            properties={
                'swagger_url': openapi.Schema(type=openapi.TYPE_STRING),
                'file': openapi.Schema(type=openapi.TYPE_FILE),
                'collection_name': openapi.Schema(type=openapi.TYPE_STRING, description="Optional name for the new collection"),
                'async': openapi.Schema(type=openapi.TYPE_BOOLEAN, default=True),
                'skip_validation': openapi.Schema(type=openapi.TYPE_BOOLEAN, default=False)
            }
        ),
        responses={201: "Created", 202: "Accepted", 400: "Bad Request"}
    )
    def post(self, request, project_id):
        project = get_object_or_404(Project, id=project_id, user=request.user)
        swagger_url = request.data.get("swagger_url")
        swagger_file = request.FILES.get("file")
        collection_name = request.data.get("collection_name") or f"Swagger Import - {timezone.now().strftime('%Y-%m-%d %H:%M')}"
        run_async = request.data.get("async", True) and HAS_CELERY
        skip_validation = request.data.get("skip_validation", False)

        if not swagger_url and not swagger_file:
            return Response({"error": "Provide swagger_url or upload a file."}, status=400)

        if run_async:
            task = import_swagger_task.delay(project.id, swagger_url, collection_name=collection_name, skip_validation=skip_validation)
            return Response({"message": "Import queued", "task_id": task.id}, status=202)

        # Sync
        try:
            raw_text = swagger_file.read().decode("utf-8") if swagger_file else fetch_spec_from_url(swagger_url)
            spec = load_spec_from_text(raw_text)
            
            if not skip_validation:
                valid, err = validate_openapi(spec)
                if not valid: return Response({"error": "Validation failed", "detail": err}, status=400)

            # Create Collection
            coll = Collection.objects.create(project=project, name=collection_name, source="swagger")
            
            from .openapi_parser import extract_base_url
            coll.base_url = extract_base_url(spec)
            coll.save()

            parse_result = parse_paths_to_endpoints(spec, project_obj=project)
            endpoints = parse_result.get("endpoints", [])
            created_count = 0
            
            for e in endpoints:
                Endpoint.objects.create(
                    collection=coll,
                    method=e["method"],
                    url=e["full_url"] or e["path"],
                    name=e["name"],
                    description=e.get("description", ""),
                    request_body=e.get("requestBody") or {},
                    query_params={}, 
                    headers={}
                )
                created_count += 1

            return Response({"message": f"Imported {created_count} endpoints into collection '{collection_name}'"}, status=201)
        except Exception as e:
            traceback.print_exc()
            return Response({"error": str(e)}, status=500)


class CrawlerImportView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    @swagger_auto_schema(
        operation_description="Crawl a website and import pages as endpoints into a new collection",
        request_body=openapi.Schema(
            type=openapi.TYPE_OBJECT,
            required=['url'],
            properties={
                'url': openapi.Schema(type=openapi.TYPE_STRING),
                'collection_name': openapi.Schema(type=openapi.TYPE_STRING),
                'max_pages': openapi.Schema(type=openapi.TYPE_INTEGER, default=50),
                'async': openapi.Schema(type=openapi.TYPE_BOOLEAN, default=True)
            }
        )
    )
    def post(self, request, project_id):
        project = get_object_or_404(Project, id=project_id, user=request.user)
        start_url = request.data.get("url")
        collection_name = request.data.get("collection_name") or f"Crawler Import - {timezone.now().strftime('%Y-%m-%d %H:%M')}"
        max_pages = int(request.data.get("max_pages", 50))
        run_async = request.data.get("async", True) and HAS_CELERY

        if not start_url: return Response({"error": "url is required"}, status=400)

        if run_async:
            task = import_crawler_task.delay(project.id, start_url, collection_name=collection_name, max_pages=max_pages)
            return Response({"message": "Crawler started", "task_id": task.id}, status=202)

        try:
            endpoints = crawl_url(start_url, max_pages=max_pages)
            coll = Collection.objects.create(project=project, name=collection_name, source="crawler", base_url=start_url)
            
            for e in endpoints:
                Endpoint.objects.create(
                    collection=coll,
                    method=e["method"],
                    url=e["full_url"],
                    name=e["name"],
                    description=e.get("description", "")
                )
            
            return Response({"message": f"Crawled and imported {len(endpoints)} pages"}, status=201)
        except Exception as e:
            return Response({"error": str(e)}, status=500)

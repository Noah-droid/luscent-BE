from rest_framework import permissions, status
from rest_framework.views import APIView
from rest_framework.response import Response
from django.shortcuts import get_object_or_404
from django.utils import timezone
from drf_yasg.utils import swagger_auto_schema
from drf_yasg import openapi
import traceback

from .models import Collection, Endpoint
from .serializers import CollectionSerializer, EndpointSerializer
from projects.models import Project
from .openapi_parser import (
    fetch_spec_from_url, load_spec_from_text, validate_openapi, 
    parse_paths_to_endpoints, extract_base_url
)
from .crawler import crawl_url

try:
    from .tasks import import_swagger_task, import_crawler_task  
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


class SwaggerImportView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    @swagger_auto_schema(
        operation_description="Import endpoints from Swagger/OpenAPI into an EXISTING collection",
        request_body=openapi.Schema(
            type=openapi.TYPE_OBJECT,
            properties={
                'swagger_url': openapi.Schema(type=openapi.TYPE_STRING),
                'file': openapi.Schema(type=openapi.TYPE_FILE),
                'async': openapi.Schema(type=openapi.TYPE_BOOLEAN, default=True),
                'skip_validation': openapi.Schema(type=openapi.TYPE_BOOLEAN, default=False)
            }
        ),
        responses={201: "Created", 202: "Accepted", 400: "Bad Request"}
    )
    def post(self, request, collection_id):
        collection = get_object_or_404(Collection, id=collection_id, project__user=request.user)
        swagger_url = request.data.get("swagger_url")
        swagger_file = request.FILES.get("file")
        run_async = request.data.get("async", True) and HAS_CELERY
        skip_validation = request.data.get("skip_validation", False)

        if not swagger_url and not swagger_file:
            return Response({"error": "Provide swagger_url or upload a file."}, status=400)

        if run_async:
            if not swagger_url:
                return Response({"error": "Async import only works with swagger_url, not files yet."}, status=400)
            task = import_swagger_task.delay(collection.id, swagger_url, skip_validation=skip_validation)
            return Response({"message": "Import queued", "task_id": task.id}, status=202)

        # Sync
        try:
            raw_text = swagger_file.read().decode("utf-8") if swagger_file else fetch_spec_from_url(swagger_url)
            spec = load_spec_from_text(raw_text)
            
            if not skip_validation:
                valid, err = validate_openapi(spec)
                if not valid: return Response({"error": "Validation failed", "detail": err}, status=400)

            # Update Collection
            collection.source = "swagger"
            spec_base_url = extract_base_url(spec)
            if spec_base_url and not collection.base_url:
                collection.base_url = spec_base_url
            collection.save()

            parse_result = parse_paths_to_endpoints(spec, project_obj=collection.project, default_base_url=collection.base_url)
            endpoints = parse_result.get("endpoints", [])
            created_count = 0
            
            for e in endpoints:
                Endpoint.objects.update_or_create(
                    collection=collection,
                    method=e["method"],
                    url=e["full_url"] or e["path"],
                    defaults={
                        "name": e["name"],
                        "description": e.get("description", ""),
                        "request_body": e.get("requestBody") or {}
                    }
                )
                created_count += 1

            return Response({"message": f"Imported {created_count} endpoints into collection '{collection.name}'"}, status=201)
        except Exception as e:
            traceback.print_exc()
            return Response({"error": str(e)}, status=500)


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

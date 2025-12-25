from rest_framework import generics, permissions, status
from .models import Collection
from rest_framework.views import APIView
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from django.shortcuts import get_object_or_404
from .serializers import CollectionSerializer
from projects.models import Project
from .openapi_parser import (
    fetch_spec_from_url, load_spec_from_text, validate_openapi, parse_paths_to_endpoints
)
from .crawler import crawl_url
import traceback



try:
    from .tasks import import_swagger_task, import_crawler_task  
    HAS_CELERY = True
except Exception:
    HAS_CELERY = False


    

class CollectionListCreateView(generics.ListCreateAPIView):
    serializer_class = CollectionSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        project_id = self.kwargs["project_id"]
        return Collection.objects.filter(project__id=project_id,
                                       project__user=self.request.user)

    def perform_create(self, serializer):
        project_id = self.kwargs["project_id"]

        project = Project.objects.filter(id=project_id,
                                         user=self.request.user).first()
        if not project:
            raise PermissionError("Project not found or not yours.")

        serializer.save(project=project)




class CollectionDetailView(generics.RetrieveUpdateDestroyAPIView):
    serializer_class = CollectionSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        return Collection.objects.filter(project__user=self.request.user)







class SwaggerImportView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, project_id):
        """
        POST payload: form-data or JSON
        - swagger_url: (optional) URL to swagger/openapi file
        - file: (optional) file upload (json or yaml)
        - async: (optional) boolean (default true if celery available)
        """
        project = get_object_or_404(Project, id=project_id, user=request.user)

        swagger_url = request.data.get("swagger_url")
        swagger_file = request.FILES.get("file")
        run_async = request.data.get("async", True) and HAS_CELERY

        if not swagger_url and not swagger_file:
            return Response({"error": "Provide swagger_url or upload a file."}, status=status.HTTP_400_BAD_REQUEST)

        if run_async and not HAS_CELERY:
            run_async = False

        if run_async:
            # queue a background task
            task = import_swagger_task.delay(project.id, swagger_url)
            return Response({"message": "Swagger import queued", "task_id": task.id}, status=status.HTTP_202_ACCEPTED)

        # synchronous path: fetch/parse/run immediately
        try:
            if swagger_file:
                raw_text = swagger_file.read().decode("utf-8")
            else:
                raw_text = fetch_spec_from_url(swagger_url)

            spec = load_spec_from_text(raw_text)

            valid, validation_error = validate_openapi(spec)
            if not valid:
                return Response({"error": "Spec validation failed", "detail": validation_error}, status=status.HTTP_400_BAD_REQUEST)

            # Pass None as default_base_url since project doesn't have it anymore
            parse_result = parse_paths_to_endpoints(spec, project_obj=project, default_base_url=None)
            created, skipped, errors = 0, parse_result.get("skipped", 0), parse_result.get("errors", [])
            endpoints = parse_result.get("endpoints", [])

            # persist endpoints
            for e in endpoints:
                try:
                    # Use full_url from parser
                    tgt_url = e.get("full_url") or e.get("path") # Fallback to path if base_url missing
                    
                    endpoint_obj, created_flag = Collection.objects.get_or_create(
                        project=project,
                        method=e["method"],
                        url=tgt_url,
                        defaults={
                            "name": e["name"],
                            "source": "swagger",
                            "query_params": {}, 
                            "headers": {},
                            "request_body": e.get("requestBody") or {},
                            "description": e.get("description", "")
                        }
                    )
                    if created_flag:
                        created += 1
                except Exception as ee:
                    errors.append(f"DB create error for {e['method']} {e['path']}: {str(ee)}")

            return Response({
                "imported": created,
                "skipped": skipped,
                "errors": errors
            }, status=status.HTTP_201_CREATED)

        except Exception as exc:
            traceback.print_exc()
            return Response({"error": "Failed to import swagger", "detail": str(exc)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


class CrawlerImportView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, project_id):
        """
        POST payload:
        - url: start URL
        - max_pages: optional int
        - async: optional bool
        """
        project = get_object_or_404(Project, id=project_id, user=request.user)
        
        start_url = request.data.get("url")
        if not start_url:
             return Response({"error": "url is required"}, status=status.HTTP_400_BAD_REQUEST)
        
        max_pages = int(request.data.get("max_pages", 50))
        run_async = request.data.get("async", True) and HAS_CELERY
        
        if run_async and not HAS_CELERY:
            run_async = False
            
        if run_async:
            task = import_crawler_task.delay(project.id, start_url, max_pages)
            return Response({"message": "Crawler started", "task_id": task.id}, status=status.HTTP_202_ACCEPTED)

        # Synchronous
        try:
            endpoints = crawl_url(start_url, max_pages=max_pages)
            created_count = 0
            errors = []
            
            for e in endpoints:
                try:
                    # Use full_url from crawler result
                    tgt_url = e.get("full_url")
                    if not tgt_url:
                        # Should not happen given crawler logic, but fallback
                        tgt_url = e["path"]

                    obj, created = Collection.objects.get_or_create(
                         project=project,
                         method=e["method"],
                         url=tgt_url,
                         defaults={
                             "name": e["name"],
                             "description": e.get("description", ""),
                             "source": "crawler"
                         }
                    )
                    if created:
                        created_count += 1
                except Exception as ee:
                    errors.append(str(ee))
            
            return Response({
                "imported": created_count,
                "found": len(endpoints),
                "errors": errors
            }, status=status.HTTP_201_CREATED)
            
        except Exception as exc:
            traceback.print_exc()
            return Response({"error": "Crawler failed", "detail": str(exc)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)







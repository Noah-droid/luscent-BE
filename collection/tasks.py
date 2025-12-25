from celery import shared_task
from django.shortcuts import get_object_or_404
from .models import Project
from .openapi_parser import fetch_spec_from_url, load_spec_from_text, validate_openapi, parse_paths_to_endpoints
from .models import Collection
import logging

logger = logging.getLogger(__name__)

@shared_task(bind=True, soft_time_limit=120)
def import_swagger_task(self, project_id, swagger_url=None):
    """
    Celery task to fetch and import swagger spec into endpoints.
    swagger_url may be None if file upload path is used (we don't queue files via celery in this pattern).
    """
    project = get_object_or_404(Project, id=project_id)
    if not swagger_url:
        return {"error": "No swagger_url provided to task."}

    try:
        raw_text = fetch_spec_from_url(swagger_url)
        spec = load_spec_from_text(raw_text)
        valid, validation_error = validate_openapi(spec)
        if not valid:
            return {"error": "Spec validation failed", "detail": validation_error}

        parse_result = parse_paths_to_endpoints(spec, project_obj=project, default_base_url=project.base_url)
        created, skipped, errors = 0, parse_result.get("skipped", 0), parse_result.get("errors", [])
        endpoints = parse_result.get("endpoints", [])

        for e in endpoints:
            try:
                endpoint_obj, created_flag = Collection.objects.get_or_create(
                    project=project,
                    method=e["method"],
                    path=e["path"],
                    defaults={
                        "name": e["name"],
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

        result = {"imported": created, "skipped": skipped, "errors": errors}
        logger.info("Swagger import finished for project %s: %s", project_id, result)
        return result

    except Exception as exc:
        logger.exception("Swagger import failed for project %s", project_id)
        return {"error": str(exc)}


@shared_task(bind=True, soft_time_limit=300)
def import_crawler_task(self, project_id, start_url, max_pages=50):
    """
    Celery task to crawl a website and import found endpoints (pages).
    """
    from .crawler import crawl_url  # local import

    project = get_object_or_404(Project, id=project_id)
    if not start_url:
        return {"error": "No start_url provided."}

    try:
        endpoints = crawl_url(start_url, max_pages=max_pages)
        created_count = 0
        errors = []

        for e in endpoints:
            try:
                # Basic get_or_create
                # We set source="crawler"
                obj, created = Collection.objects.get_or_create(
                    project=project,
                    method=e["method"],
                    path=e["path"],
                    defaults={
                        "name": e["name"],
                        "description": e.get("description", ""),
                        "source": "crawler"
                    }
                )
                if created:
                    created_count += 1
            except Exception as ee:
                errors.append(f"Error saving {e['path']}: {str(ee)}")

        result = {"imported": created_count, "found": len(endpoints), "errors": errors}
        logger.info("Crawler finished for project %s: %s", project_id, result)
        return result

    except Exception as exc:
        logger.exception("Crawler failed for project %s", project_id)
        return {"error": str(exc)}


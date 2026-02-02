from celery import shared_task
from django.shortcuts import get_object_or_404
from projects.models import Project
from .models import Collection, Endpoint
from .openapi_parser import (
    fetch_spec_from_url, load_spec_from_text, validate_openapi, 
    parse_paths_to_endpoints, extract_base_url
)
import logging
import traceback

logger = logging.getLogger(__name__)

@shared_task(bind=True, soft_time_limit=120)
def import_swagger_task(self, collection_id, swagger_url, skip_validation=False):
    """
    Celery task to fetch and import swagger spec into an existing collection.
    """
    coll = get_object_or_404(Collection, id=collection_id)
    project = coll.project

    try:
        raw_text = fetch_spec_from_url(swagger_url)
        spec = load_spec_from_text(raw_text)
        
        if not skip_validation:
            valid, validation_error = validate_openapi(spec)
            if not valid:
                return {"error": "Spec validation failed", "detail": validation_error}

        # Update Collection metadata
        coll.source = "swagger"
        coll.base_url = extract_base_url(spec)
        coll.save()

        parse_result = parse_paths_to_endpoints(spec, project_obj=project)
        created, errors = 0, parse_result.get("errors", [])
        endpoints = parse_result.get("endpoints", [])

        for e in endpoints:
            try:
                # Use update_or_create to avoid duplicates if re-imported
                Endpoint.objects.update_or_create(
                    collection=coll,
                    method=e["method"],
                    url=e["full_url"] or e["path"],
                    defaults={
                        "name": e["name"],
                        "description": e.get("description", ""),
                        "request_body": e.get("requestBody") or {}
                    }
                )
                created += 1
            except Exception as ee:
                errors.append(f"DB error for {e.get('method')} {e.get('path')}: {str(ee)}")

        return {"imported": created, "errors": errors, "collection_id": coll.id}

    except Exception as exc:
        logger.exception("Swagger import failed")
        return {"error": str(exc)}


@shared_task(bind=True, soft_time_limit=300)
def import_crawler_task(self, collection_id, start_url, max_pages=50):
    """
    Celery task to crawl a website and import found endpoints into an existing collection.
    """
    from .crawler import crawl_url
    coll = get_object_or_404(Collection, id=collection_id)

    try:
        endpoints = crawl_url(start_url, max_pages=max_pages)
        
        # Update Collection metadata
        coll.source = "crawler"
        coll.base_url = start_url
        coll.save()

        created_count = 0
        for e in endpoints:
            try:
                Endpoint.objects.update_or_create(
                    collection=coll,
                    method=e["method"],
                    url=e["full_url"],
                    defaults={
                        "name": e["name"],
                        "description": e.get("description", "")
                    }
                )
                created_count += 1
            except Exception:
                pass

        return {"imported": created_count, "collection_id": coll.id}

    except Exception as exc:
        logger.exception("Crawler failed")
        return {"error": str(exc)}

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
def import_swagger_task(self, project_id, swagger_url, collection_name=None, skip_validation=False):
    """
    Celery task to fetch and import swagger spec into a new collection.
    """
    project = get_object_or_404(Project, id=project_id)
    if not collection_name:
        collection_name = f"Swagger Import - {swagger_url[:30]}"

    try:
        raw_text = fetch_spec_from_url(swagger_url)
        spec = load_spec_from_text(raw_text)
        
        if not skip_validation:
            valid, validation_error = validate_openapi(spec)
            if not valid:
                return {"error": "Spec validation failed", "detail": validation_error}

        # Create Collection
        coll = Collection.objects.create(
            project=project, 
            name=collection_name, 
            source="swagger",
            base_url=extract_base_url(spec)
        )

        parse_result = parse_paths_to_endpoints(spec, project_obj=project)
        created, errors = 0, parse_result.get("errors", [])
        endpoints = parse_result.get("endpoints", [])

        for e in endpoints:
            try:
                Endpoint.objects.create(
                    collection=coll,
                    method=e["method"],
                    url=e["full_url"] or e["path"],
                    name=e["name"],
                    description=e.get("description", ""),
                    request_body=e.get("requestBody") or {}
                )
                created += 1
            except Exception as ee:
                errors.append(f"DB error for {e.get('method')} {e.get('path')}: {str(ee)}")

        return {"imported": created, "errors": errors, "collection_id": coll.id}

    except Exception as exc:
        logger.exception("Swagger import failed")
        return {"error": str(exc)}


@shared_task(bind=True, soft_time_limit=300)
def import_crawler_task(self, project_id, start_url, collection_name=None, max_pages=50):
    """
    Celery task to crawl a website and import found endpoints into a collection.
    """
    from .crawler import crawl_url
    project = get_object_or_404(Project, id=project_id)
    if not collection_name:
        collection_name = f"Crawler Import - {start_url}"

    try:
        endpoints = crawl_url(start_url, max_pages=max_pages)
        
        coll = Collection.objects.create(
            project=project, 
            name=collection_name, 
            source="crawler",
            base_url=start_url
        )

        created_count = 0
        for e in endpoints:
            try:
                Endpoint.objects.create(
                    collection=coll,
                    method=e["method"],
                    url=e["full_url"],
                    name=e["name"],
                    description=e.get("description", "")
                )
                created_count += 1
            except Exception:
                pass

        return {"imported": created_count, "collection_id": coll.id}

    except Exception as exc:
        logger.exception("Crawler failed")
        return {"error": str(exc)}

from celery import shared_task
from django.shortcuts import get_object_or_404
from .models import Collection, Endpoint, ImportJob
from .importer import run_swagger_import
import logging

logger = logging.getLogger(__name__)


@shared_task(name="import_swagger", bind=True, soft_time_limit=120)
def import_swagger_task(self, job_id):
    """Celery task to run a swagger ImportJob (URL or inline file payload)."""
    job = get_object_or_404(ImportJob, id=job_id)
    result = run_swagger_import(job)
    logger.info("Import job %s finished: %s", job_id, result)
    return result


@shared_task(name="import_crawler", bind=True, soft_time_limit=300)
def import_crawler_task(self, collection_id, start_url, max_pages=50):
    """Celery task to crawl a website and import found endpoints into an existing collection."""
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

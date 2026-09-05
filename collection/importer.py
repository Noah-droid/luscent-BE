"""
Shared execution + queueing for collection imports (Swagger/OpenAPI).

An ImportJob row is the source of truth for progress: the API process creates it,
a Celery worker (or an inline fallback when Celery is unreachable) runs it and
updates status/counts/errors. Keeping the payload on the row means file uploads
work even when the API and Celery containers don't share a filesystem.
"""
import logging
import threading
import time
from datetime import timedelta

from django.conf import settings
from django.utils import timezone

from .models import Collection, Endpoint, ImportJob
from .openapi_parser import (
    fetch_spec_from_url,
    load_spec_from_text,
    validate_openapi,
    extract_base_url,
    parse_paths_to_endpoints,
)

logger = logging.getLogger(__name__)

__all__ = ["queue_swagger_import", "run_swagger_import", "reap_stale_jobs"]

# How long an ImportJob may stay queued/running before we consider its worker dead
# (OOM-killed, container restarted, broker hiccup...) and stop treating it as active.
# Far beyond Celery's own soft time limit (120s), so a slow-but-live run is never
# touched; only genuinely orphaned rows are reaped.
STALE_JOB_AFTER_SECONDS = 10 * 60


def run_swagger_import(job):
    """
    Execute a swagger ImportJob end-to-end and persist the outcome on the row.
    Safe to call from a Celery worker or inline (request process).
    """
    if not isinstance(job, ImportJob) or job.status in ("success", "failed"):
        return {"skipped": True}

    # A "running" row means another process owns the job. Back off while it looks
    # alive; if it has been running far longer than any import should take, the
    # previous worker died mid-run (e.g. OOM-killed) — fail the stale row and then
    # re-run cleanly below.
    if job.status == "running":
        if job.started_at and (timezone.now() - job.started_at).total_seconds() < STALE_JOB_AFTER_SECONDS:
            return {"skipped": True, "reason": "job already running"}
        job.status = "failed"
        job.error = "Import was interrupted (worker stopped). Re-running."
        job.finished_at = timezone.now()
        job.save(update_fields=["status", "error", "finished_at"])

    job.status = "running"
    job.started_at = timezone.now()
    job.save(update_fields=["status", "started_at"])

    coll = job.collection
    project = coll.project

    try:
        raw_text = job.spec_text if job.spec_text else fetch_spec_from_url(job.spec_name)
        spec = load_spec_from_text(raw_text)

        if not job.skip_validation:
            valid, validation_error = validate_openapi(spec)
            if not valid:
                raise ValueError(f"Spec validation failed: {validation_error}")

        # Base URL precedence: project target_url > spec server URL > existing collection URL
        spec_base_url = extract_base_url(spec)
        if project.target_url:
            coll.base_url = project.target_url
        elif spec_base_url and not coll.base_url:
            coll.base_url = spec_base_url
        coll.source = "swagger"
        coll.save()

        parse_result = parse_paths_to_endpoints(
            spec, project_obj=project, default_base_url=coll.base_url
        )
        endpoints = parse_result.get("endpoints", [])

        imported = 0
        errors = []
        for e in endpoints:
            try:
                Endpoint.objects.update_or_create(
                    collection=coll,
                    method=e["method"],
                    url=e["full_url"] or e["path"],
                    defaults={
                        "name": e["name"],
                        "description": e.get("description", ""),
                        "request_body": e.get("flattened_body") or e.get("requestBody") or {},
                        "query_params": e.get("flattened_query") or {},
                    },
                )
                imported += 1
            except Exception as ee:  # noqa: BLE001 - one bad endpoint shouldn't kill the job
                errors.append(f"{e.get('method')} {e.get('path')}: {ee}")

        job.status = "success"
        job.imported_count = imported
        job.error = "; ".join(errors[:20])
        return {"imported": imported, "errors": errors}
    except Exception as exc:  # noqa: BLE001
        logger.exception("Swagger import job %s failed", job.id)
        job.status = "failed"
        job.error = str(exc)
        return {"error": str(exc)}
    finally:
        job.spec_text = None  # drop the payload once processed
        job.finished_at = timezone.now()
        job.save(update_fields=["status", "imported_count", "error", "spec_text", "finished_at"])


def queue_swagger_import(collection, *, source="url", spec_name="", spec_text=None, skip_validation=False):
    """
    Create an ImportJob and dispatch it to Celery. Falls back to an in-process run
    (synchronous in local dev, daemon thread on deployed hosts) when Celery isn't
    available/unreachable so imports never block the caller or silently vanish.
    Returns the job.
    """
    if source not in ("url", "file"):
        raise ValueError("source must be 'url' or 'file'")
    if not spec_name and not spec_text:
        raise ValueError("spec_name or spec_text is required")

    job = ImportJob.objects.create(
        collection=collection,
        kind="swagger",
        source=source,
        spec_name=str(spec_name)[:500],
        spec_text=spec_text,
        skip_validation=bool(skip_validation),
    )

    dispatched = False
    result = None

    # Hand the job to a Celery worker whenever one is reachable. There is no DEBUG
    # gate here: a deployed host that accidentally runs with DEBUG=True must still
    # use the queue. If no broker/worker answers, fall back to an in-process run so
    # an import never blocks the web request and never silently vanishes.
    try:
        from .tasks import import_swagger_task  # noqa: PLC0415 - deferred import

        result = import_swagger_task.delay(str(job.id))
        # Confirm a live worker actually starts the task. If nothing starts
        # (broker up but no worker, or a stale worker) fall back to an in-process
        # run so a job never sits in 'queued' forever. run_swagger_import is
        # idempotent and skips rows a live worker already owns, so a slow-but-real
        # worker racing us is harmless.
        for _ in range(6):  # up to ~1.5s
            try:
                state = result.state
            except Exception:
                state = "PENDING"
                break
            if state in ("STARTED", "SUCCESS", "RETRY"):
                dispatched = True
                break
            if state == "FAILURE":
                break  # stale/mismatched worker — let the in-process run handle it
            time.sleep(0.25)
    except Exception as exc:  # noqa: BLE001 - broker down / celery not installed
        logger.warning(
            "Celery dispatch unavailable for import job %s (%s); running in-process.", job.id, exc
        )

    if not dispatched:
        if result is not None:
            try:
                result.revoke(terminate=False)
            except Exception:  # noqa: BLE001
                pass
        if getattr(settings, "DEBUG", False):
            # Local dev: finish synchronously so results are immediate and predictable.
            run_swagger_import(job)
        else:
            # Deployed host with no reachable worker: don't make the caller wait for
            # a potentially huge spec. Parse on a daemon thread and surface progress
            # through the ImportJob row exactly like a Celery run would.
            threading.Thread(
                target=run_swagger_import,
                args=(job,),
                daemon=True,
                name=f"import-{job.id}",
            ).start()

    return job


def reap_stale_jobs(user=None, collection=None, *, seconds=STALE_JOB_AFTER_SECONDS):
    """
    Mark ImportJobs stuck in queued/running as failed once nothing finished them
    within ``seconds``. This stops orphaned rows (worker OOM-killed, container
    restarted) from 409-blocking new imports and from keeping the UI's progress
    toast spinning forever. Idempotent and cheap, so it is safe to call from read
    paths; a live-but-slow worker is never touched because the window is far
    beyond Celery's own soft time limit.
    """
    cutoff = timezone.now() - timedelta(seconds=seconds)
    qs = ImportJob.objects.filter(status__in=("queued", "running"), created_at__lt=cutoff)
    if collection is not None:
        qs = qs.filter(collection=collection)
    elif user is not None:
        qs = qs.filter(collection__project__user=user)

    stale = list(qs[:50])
    for job in stale:
        job.status = "failed"
        job.error = "Import was interrupted (no worker finished it in time). Please try again."
        job.finished_at = timezone.now()
        job.save(update_fields=["status", "error", "finished_at"])
    if stale:
        logger.warning("Reaped %d stale import job(s) older than %ss.", len(stale), seconds)
    return stale

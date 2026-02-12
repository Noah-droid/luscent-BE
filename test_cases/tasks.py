from celery import shared_task
from .runner_service import RunnerService
import logging
from django.utils import timezone

logger = logging.getLogger(__name__)
logger.info("TEST_CASES TASKS MODULE LOADING...")

@shared_task(
    name="run_test_case",
    bind=True,
    time_limit=180,
    soft_time_limit=150,
    max_retries=2,
    default_retry_delay=10
)
def run_test_case_task(self, test_case_id, override_url=None, batch_id=None, triggered_by="manual", send_notification=True):
    """
    Execute a test case with timeout and retry logic.
    
    Args:
        test_case_id: ID of the test case to run
        override_url: Optional override for the target URL
        batch_id: Optional ID for batch grouping
        triggered_by: Source of the trigger
    
    Raises:
        SoftTimeLimitExceeded: If task exceeds soft_time_limit
        TimeLimitExceeded: If task exceeds time_limit
    """
    logger.info(f"[CeleryTask] run_test_case_task started: test_case_id={test_case_id}, batch_id={batch_id}, triggered_by={triggered_by}")
    try:
        service = RunnerService()
        result = service.execute_test(
            test_case_id, 
            override_url=override_url, 
            batch_id=batch_id, 
            triggered_by=triggered_by,
            send_notification=send_notification
        )
        logger.info(f"[CeleryTask] run_test_case_task completed successfully for test_case_id={test_case_id}")
        return result
    except Exception as exc:
        # Log the error
        logger.error(f"Test case {test_case_id} failed: {exc}")
        
        # Retry on transient failures (network errors, timeouts)
        # Don't retry on validation errors or quota exceeded
        if "quota" not in str(exc).lower() and "validation" not in str(exc).lower():
            raise self.retry(exc=exc)
        else:
            # Don't retry, fail
            raise


@shared_task(name="project_auto_pilot", time_limit=1800) # 30 mins max
def project_auto_pilot_task(project_id, user_id, scenarios, batch_id, user_story=None, runner_types=["http"], categories=["functional"], layer="backend", use_visual_ai=False):
    """
    Auto-Pilot background task.
    """
    from .models import TestCase
    from collection.models import Collection
    from users.models import User
    from .ai_generator import AITestGenerator
    from billing.services import deduct_tokens, calculate_test_cost
    from projects.models import Project
    
    user = User.objects.get(id=user_id)
    project = Project.objects.get(id=project_id)
    collections = Collection.objects.filter(project=project)
    generator = AITestGenerator()

    for coll in collections:
        # If no story provided in the trigger, use the collection context or project context as base
        final_story = user_story or coll.user_story or coll.description or project.user_story or project.description or ""
        
        # Auto-Pilot for each endpoint in the collection
        endpoints = coll.endpoints.all()
        for endpoint in endpoints:
            for category in categories:
                # 1. Billing check for AI
                AI_COST = 5
                context_label = f"{category.upper()}"
                if not deduct_tokens(user, AI_COST, f"Auto-Pilot ({context_label}): {endpoint.name}"):
                    logger.warning(f"Insufficient tokens for Auto-Pilot on {endpoint.name}")
                    continue
    
                # 2. Call AI with allowed_runners list
                draft_tests = generator.generate_draft_plan(
                    endpoint,
                    scenarios=scenarios,
                    user_story=final_story,
                    allowed_runners=runner_types,
                    category=category,
                    layer=layer
                )
                
                # 3. Save Tests & Trigger Runs
                for data in draft_tests:
                    try:
                        # Determine chosen runner from AI response, fallback to HTTP
                        chosen_runner = data.get("runner_type", "http").lower()
                        
                        test_case = TestCase.objects.create(
                            endpoint=endpoint,
                            name=data.get("name"),
                            description=data.get("description"),
                            priority=data.get("priority", "medium").lower(),
                            category=category,
                            layer=data.get("layer", "backend"),
                            runner_type=chosen_runner, # Use AI choice
                            test_script=data.get("test_script"),
                            headers=data.get("headers", {}),
                            query_params=data.get("query_params", {}),
                            body=data.get("body", {}),
                            expected_status=data.get("expected_status", 200),
                            assertions=data.get("assertions", []),
                            tags=data.get("tags", []),
                            use_visual_ai=use_visual_ai,
                            ai_generated=True,
                            user_story=data.get("user_story", final_story)
                        )
                        
                        # 4. Trigger Run (Immediate Queue)
                        RUN_COST = calculate_test_cost(test_case.runner_type)
                        if deduct_tokens(user, RUN_COST, f"Auto-Pilot Run: {test_case.name}"):
                            run_test_case_task.delay(
                                test_case.id, 
                                batch_id=batch_id, 
                                triggered_by="ai",
                                send_notification=False # Skip individual emails
                            )
                    except Exception as e:
                        logger.error(f"Auto-Pilot failed to create/run test for {endpoint.name}: {e}")
    
    # Schedule batch report 
    send_batch_report_task.apply_async((str(batch_id), user.id), countdown=300) # 5m delay as AI gen takes time


@shared_task(name="collection_auto_pilot", time_limit=1800)  # 30 mins max
def collection_auto_pilot_task(collection_id, user_id, scenarios, batch_id, user_story=None, runner_types=["http"], categories=["functional"], layer="backend", use_visual_ai=False):
    """
    Collection-level Auto-Pilot background task.
    Generates and runs tests for all endpoints in a specific collection.
    Supports multiple runner types and categories.
    """
    logger.info(f"[CollectionAutoPilot] Starting for collection_id={collection_id}, batch_id={batch_id}, scenarios={scenarios}, categories={categories}")
    
    from .models import TestCase
    from collection.models import Collection
    from users.models import User
    from .ai_generator import AITestGenerator
    from billing.services import deduct_tokens, calculate_test_cost
    import itertools
    
    user = User.objects.get(id=user_id)
    collection = Collection.objects.get(id=collection_id)
    generator = AITestGenerator()

    # If no story provided, use collection context or project context
    final_story = user_story or collection.user_story or collection.description or collection.project.user_story or collection.project.description or ""
    
    # Auto-Pilot for each endpoint in the collection
    endpoints = collection.endpoints.all()
    logger.info(f"[CollectionAutoPilot] Found {endpoints.count()} endpoints to process")

    # Iterate over categories (Functional, Security, etc.)
    # We NO LONGER iterate over runner_types directly. We pass the list to the AI
    # and let the AI decide the best runner (HTTP vs Browser) for each endpoint.
    
    # REWRITTEN: ORCHESTRATED ENGINE
    generator = AITestGenerator()
    
    # If no story provided, use collection context or project context
    final_story = user_story or collection.user_story or collection.description or collection.project.user_story or collection.project.description or ""
    
    # 1. Billing check for ORCHESTRATION (Flat bulk cost for planning + generation)
    ORCHESTRATION_PLAN_COST = 25 
    if not deduct_tokens(user, ORCHESTRATION_PLAN_COST, f"Auto-Pilot Orchestration: {collection.name}"):
        logger.warning(f"Insufficient tokens for Orchestration on {collection.name}")
        return

    # 2. Trigger AI Orchestration (Planning + Test Generation in one flow)
    try:
        draft_tests = generator.orchestrate_collection_tests(
            collection,
            user_story=final_story,
            scenarios=scenarios,
            allowed_runners=runner_types
        )
        logger.info(f"[CollectionAutoPilot] AI generated {len(draft_tests)} orchestrated tests.")
    except Exception as e:
        logger.error(f"[CollectionAutoPilot] Orchestration failed: {e}")
        return

    # 3. SEQUENTIAL EXECUTION (CRITICAL for Stateful Flow)
    from .models import TestCase
    from .tasks import run_test_case_task
    import time
    
    for test_data in draft_tests:
        try:
            # Re-fetch endpoint to be safe
            endpoint_id = test_data.get("endpoint_id") or test_data.get("endpoint") 
            endpoint = collection.endpoints.get(id=endpoint_id) if endpoint_id else None
            
            if not endpoint:
                logger.warning(f"Skipping test '{test_data.get('name')}' - Endpoint not found.")
                continue

            # Create the test case record
            test_case = TestCase.objects.create(
                endpoint=endpoint,
                name=test_data.get("name"),
                description=test_data.get("description"),
                priority=test_data.get("priority", "medium").lower(),
                category=test_data.get("category", "functional"),
                layer=test_data.get("layer", layer),
                runner_type=test_data.get("runner_type", "http").lower(),
                test_script=test_data.get("test_script"),
                headers=test_data.get("headers", {}),
                query_params=test_data.get("query_params", {}),
                body=test_data.get("body", {}),
                expected_status=test_data.get("expected_status", 200),
                assertions=test_data.get("assertions", []),
                ai_generated=True,
                user_story=final_story
            )

            # Execution Billing
            RUN_COST = calculate_test_cost(test_case.runner_type)
            if deduct_tokens(user, RUN_COST, f"Stateful Run: {test_case.name}"):
                logger.info(f"[CollectionAutoPilot] Executing sequence step: {test_case.name}")
                
                # RUN SYNCHRONOUSLY (By calling the service directly or using .run())
                # To ensure state is passed, we run them one by one in THIS thread
                # or we could chain them, but a loop here is easiest to manage.
                from .runner_service import RunnerService
                runner = RunnerService()
                runner.execute_test(test_case, batch_id=batch_id, triggered_by="ai")
                
                # Tiny cooldown to allow DB/Runner state to settle
                time.sleep(2)
            else:
                logger.warning(f"Task skipped due to tokens: {test_case.name}")

        except Exception as ex:
            logger.error(f"Failed to execute orchestrated step: {ex}")
            continue
    
    # Schedule batch report
    send_batch_report_task.apply_async((str(batch_id), user.id), countdown=180) # 3m delay
    
    logger.info(f"[CollectionAutoPilot] Completed for collection_id={collection_id}, batch_id={batch_id}")
    
    logger.info(f"[CollectionAutoPilot] Completed for collection_id={collection_id}, batch_id={batch_id}")


@shared_task(name="send_batch_report_task")
def send_batch_report_task(batch_id, user_id):
    """
    Asynchronous task to collect results and send a single batch report.
    Wait a bit to ensure all tasks in the batch have likely finished.
    """
    import time
    from users.models import User
    from .models import TestRun
    from notifications.services import send_batch_report
    
    # Wait for completion (simple polling for 30s max)
    user = User.objects.get(id=user_id)
    
    # Give it some time for the last tasks to finish
    time.sleep(10) 
    
    send_batch_report(batch_id, user)


@shared_task(name="check_periodic_schedules")
def check_periodic_schedules_task():
    """
    Background worker that runs scanned for scheduled collections.
    Uses a cache lock to prevent overlapping runs.
    """
    from django.core.cache import cache
    from collection.models import Collection
    from billing.services import deduct_tokens, calculate_test_cost
    from django.db.models import Q
    import uuid

    # Distributed lock to prevent Task Overlap (snowball effect)
    lock_id = "qai_periodic_check_lock"
    # Acquire lock for 10 minutes max
    if not cache.add(lock_id, "true", 600):
        logger.info("[PeriodicCheck] Another instance is already running. Skipping.")
        return

    try:
        now = timezone.now()
        scheduled_collections = Collection.objects.filter(is_scheduled=True)

        for collection in scheduled_collections:
            is_due = False
            if not collection.last_scheduled_run_at:
                is_due = True
            else:
                diff = now - collection.last_scheduled_run_at
                if diff.total_seconds() / 60 >= collection.schedule_interval - 0.1:
                    is_due = True

            if is_due:
                # IMPORTANT: Update timestamp IMMEDIATELY to prevent overlap
                collection.last_scheduled_run_at = now
                collection.save(update_fields=['last_scheduled_run_at'])
                
                logger.info(f"Triggering scheduled period check for: {collection.name}")
                batch_id = uuid.uuid4()
                endpoints = collection.endpoints.all()
                user = collection.project.user
                
                run_count = 0
                for endpoint in endpoints:
                    # Only run high-priority/smoke tests for scheduled checks
                    test_cases = endpoint.test_cases.filter(
                        Q(priority='high') | Q(priority='critical') | Q(category='smoke')
                    )
                    if not test_cases.exists():
                        test_cases = endpoint.test_cases.all()[:1]

                    for test_case in test_cases:
                        cost = calculate_test_cost(test_case.runner_type)
                        if deduct_tokens(user, cost, f"Scheduled Check: {test_case.name}"):
                            run_test_case_task.delay(
                                test_case.id,
                                batch_id=batch_id,
                                triggered_by="scheduled",
                                send_notification=False 
                            )
                            run_count += 1
                
                if run_count > 0:
                     send_batch_report_task.apply_async((str(batch_id), user.id), countdown=60)
    
    finally:
        cache.delete(lock_id)

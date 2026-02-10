from celery import shared_task
from .runner_service import RunnerService
import logging
from django.utils import timezone

logger = logging.getLogger(__name__)
logger.info("TEST_CASES TASKS MODULE LOADING...")

@shared_task(
    name="run_test_case",
    bind=True,
    time_limit=180,        # Hard timeout: 3 minutes (kills task)
    soft_time_limit=150,   # Soft timeout: 2.5 minutes (raises exception)
    max_retries=2,         # Retry up to 2 times on failure
    default_retry_delay=10 # Wait 10 seconds between retries
)
def run_test_case_task(self, test_case_id, override_url=None, batch_id=None, triggered_by="manual"):
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
            triggered_by=triggered_by
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
                                triggered_by="ai"
                            )
                    except Exception as e:
                        logger.error(f"Auto-Pilot failed to create/run test for {endpoint.name}: {e}")


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
    
    for endpoint in endpoints:
        logger.info(f"[CollectionAutoPilot] Processing endpoint: {endpoint.name} ({endpoint.method} {endpoint.url})")
        for category in categories:
            logger.info(f"[CollectionAutoPilot] Generating {category} tests for endpoint: {endpoint.name}")
            
            # 1. Billing check for AI
            AI_COST = 5
            context_label = f"{category.upper()}"
            if not deduct_tokens(user, AI_COST, f"Auto-Pilot ({context_label}): {endpoint.name}"):
                logger.warning(f"Insufficient tokens for Auto-Pilot on {endpoint.name}")
                continue

            # 2. Call AI with allowed_runners list
            try:
                draft_tests = generator.generate_draft_plan(
                    endpoint,
                    scenarios=scenarios,
                    user_story=final_story,
                    allowed_runners=runner_types, # Pass the list!
                    category=category,
                    layer=layer
                )
                logger.info(f"[CollectionAutoPilot] AI generated {len(draft_tests)} tests for {endpoint.name}")
            except Exception as e:
                logger.error(f"[CollectionAutoPilot] AI generation failed for {endpoint.name}: {e}")
                continue
            
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
                        layer=data.get("layer", layer),
                        runner_type=chosen_runner, # Use AI's choice
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
                    logger.info(f"[CollectionAutoPilot] Created test case: {test_case.name} (id={test_case.id})")
                    
                    # 4. Trigger Run (Immediate Queue)
                    RUN_COST = calculate_test_cost(test_case.runner_type)
                    if deduct_tokens(user, RUN_COST, f"Auto-Pilot Run: {test_case.name}"):
                        logger.info(f"[CollectionAutoPilot] Queueing test run for: {test_case.name}")
                        run_test_case_task.delay(
                            test_case.id, 
                            batch_id=batch_id, 
                            triggered_by="ai"
                        )
                    else:
                        logger.warning(f"[CollectionAutoPilot] Insufficient tokens to run test: {test_case.name}")
                except Exception as e:
                    logger.error(f"Collection Auto-Pilot failed to create/run test for {endpoint.name}: {e}")
    
    logger.info(f"[CollectionAutoPilot] Completed for collection_id={collection_id}, batch_id={batch_id}")


@shared_task(name="check_periodic_schedules")
def check_periodic_schedules_task():
    """
    Background worker that runs every minute to scan for collections
    that are due for a 'period check' (uptime monitoring).
    """
    from collection.models import Collection
    from billing.services import deduct_tokens, calculate_test_cost
    import uuid

    now = timezone.now()
    # Find all scheduled collections
    scheduled_collections = Collection.objects.filter(is_scheduled=True)

    for collection in scheduled_collections:
        # Check if it's due
        # Logic: (now - last_run) >= interval_minutes
        is_due = False
        if not collection.last_scheduled_run_at:
            is_due = True
        else:
            diff = now - collection.last_scheduled_run_at
            if diff.total_seconds() / 60 >= collection.schedule_interval - 0.1: # 0.1 buffer for slight delays
                is_due = True

        if is_due:
            logger.info(f"Triggering scheduled period check for: {collection.name}")
            batch_id = uuid.uuid4()
            endpoints = collection.endpoints.all()
            
            for endpoint in endpoints:
                for test_case in endpoint.test_cases.all():
                    # Billing check for the run
                    user = collection.project.user
                    cost = calculate_test_cost(test_case.runner_type)
                    
                    if deduct_tokens(user, cost, f"Scheduled Check: {test_case.name}"):
                        run_test_case_task.delay(
                            test_case.id,
                            batch_id=batch_id,
                            triggered_by="scheduled"
                        )
            
            # Update last run timestamp
            collection.last_scheduled_run_at = now
            collection.save(update_fields=['last_scheduled_run_at'])

# LEGACY SHIM: Support old task name for environments still using the old scheduler
@shared_task(name="test_cases.tasks.check_periodic_schedules_task")
def legacy_check_periodic_schedules_task():
    logger.info("Legacy periodic task name triggered, redirecting to new name.")
    return check_periodic_schedules_task()

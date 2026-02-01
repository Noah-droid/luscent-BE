from celery import shared_task
from .runner_service import RunnerService
import logging

logger = logging.getLogger(__name__)

@shared_task(
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
    try:
        service = RunnerService()
        service.execute_test(
            test_case_id, 
            override_url=override_url, 
            batch_id=batch_id, 
            triggered_by=triggered_by
        )
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


@shared_task(time_limit=1800) # 30 mins max
def project_auto_pilot_task(project_id, user_id, scenarios, batch_id, user_story=None):
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

    # If no story provided in the trigger, use the project description as base context
    final_story = user_story or project.description or ""
    
    for coll in collections:
        # Auto-Pilot for each endpoint in the collection
        endpoints = coll.endpoints.all()
        for endpoint in endpoints:
            # 1. Billing check for AI
            AI_COST = 5
            if not deduct_tokens(user, AI_COST, f"Auto-Pilot Gen: {endpoint.name}"):
                logger.warning(f"Insufficient tokens for Auto-Pilot on {endpoint.name}")
                continue

            # 2. Call AI
            draft_tests = generator.generate_draft_plan(
                endpoint,
                scenarios=scenarios,
                user_story=final_story
            )
            
            # 3. Save Tests & Trigger Runs
            for data in draft_tests:
                try:
                    test_case = TestCase.objects.create(
                        endpoint=endpoint,
                        name=data.get("name"),
                        description=data.get("description"),
                        priority=data.get("priority", "medium").lower(),
                        category=data.get("category", "functional"),
                        layer=data.get("layer", "backend"),
                        runner_type=data.get("runner_type", "http"),
                        test_script=data.get("test_script"),
                        headers=data.get("headers", {}),
                        query_params=data.get("query_params", {}),
                        body=data.get("body", {}),
                        expected_status=data.get("expected_status", 200),
                        assertions=data.get("assertions", []),
                        tags=data.get("tags", []),
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


@shared_task(time_limit=1800)  # 30 mins max
def collection_auto_pilot_task(collection_id, user_id, scenarios, batch_id, user_story=None):
    """
    Collection-level Auto-Pilot background task.
    Generates and runs tests for all endpoints in a specific collection.
    """
    from .models import TestCase
    from collection.models import Collection
    from users.models import User
    from .ai_generator import AITestGenerator
    from billing.services import deduct_tokens, calculate_test_cost
    
    user = User.objects.get(id=user_id)
    collection = Collection.objects.get(id=collection_id)
    generator = AITestGenerator()

    # If no story provided, use collection description or project description
    final_story = user_story or collection.description or collection.project.description or ""
    
    # Auto-Pilot for each endpoint in the collection
    endpoints = collection.endpoints.all()
    for endpoint in endpoints:
        # 1. Billing check for AI
        AI_COST = 5
        if not deduct_tokens(user, AI_COST, f"Collection Auto-Pilot Gen: {endpoint.name}"):
            logger.warning(f"Insufficient tokens for Collection Auto-Pilot on {endpoint.name}")
            continue

        # 2. Call AI
        draft_tests = generator.generate_draft_plan(
            endpoint,
            scenarios=scenarios,
            user_story=final_story
        )
        
        # 3. Save Tests & Trigger Runs
        for data in draft_tests:
            try:
                test_case = TestCase.objects.create(
                    endpoint=endpoint,
                    name=data.get("name"),
                    description=data.get("description"),
                    priority=data.get("priority", "medium").lower(),
                    category=data.get("category", "functional"),
                    layer=data.get("layer", "backend"),
                    runner_type=data.get("runner_type", "http"),
                    test_script=data.get("test_script"),
                    headers=data.get("headers", {}),
                    query_params=data.get("query_params", {}),
                    body=data.get("body", {}),
                    expected_status=data.get("expected_status", 200),
                    assertions=data.get("assertions", []),
                    tags=data.get("tags", []),
                    ai_generated=True,
                    user_story=data.get("user_story", final_story)
                )
                
                # 4. Trigger Run (Immediate Queue)
                RUN_COST = calculate_test_cost(test_case.runner_type)
                if deduct_tokens(user, RUN_COST, f"Collection Auto-Pilot Run: {test_case.name}"):
                    run_test_case_task.delay(
                        test_case.id, 
                        batch_id=batch_id, 
                        triggered_by="ai"
                    )
            except Exception as e:
                logger.error(f"Collection Auto-Pilot failed to create/run test for {endpoint.name}: {e}")



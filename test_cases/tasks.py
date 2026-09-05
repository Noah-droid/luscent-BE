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
                AI_COST = calculate_test_cost('ai_generation')
                context_label = f"{category.upper()}"
                if not deduct_tokens(user, AI_COST, f"Auto-Pilot ({context_label}): {endpoint.name}"):
                    logger.warning(f"Insufficient tokens for Auto-Pilot on {endpoint.name}")
                    # If we had a mission, we'd error it, but project auto-pilot is a generator.
                    # We can at least log it.
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
def collection_auto_pilot_task(collection_id, user_id, scenarios, batch_id, user_story=None, runner_types=["http"], categories=["functional"], layer="backend", use_visual_ai=False, mission_id=None):
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
    

    from .autonomous_agent import AutonomousAgent
    from .models import TestCase, TestRun, AgentMission
    import json
    
    # If no story provided, use collection context or project context
    final_story = user_story or collection.user_story or collection.description or collection.project.user_story or collection.project.description or ""
    
    # 1.5 Create Agent Mission (For Live Tracking) - IF NOT ALREADY CREATED BY VIEW
    if mission_id:
        mission = AgentMission.objects.get(id=mission_id)
    else:
        mission = AgentMission.objects.create(
            user=user,
            collection=collection,
            user_story=final_story,
            batch_id=batch_id,
            status="pending"
        )

    # 1. Billing check for AGENT SESSION (Hybrid: Entry Fee)
    AGENT_ENTRY_COST = calculate_test_cost('agent_mission_entry') 
    if not deduct_tokens(user, AGENT_ENTRY_COST, f"Autonomous Agent Entry: {collection.name}"):
        logger.warning(f"Insufficient tokens for Agent Entry on {collection.name}")
        mission.status = "error"
        mission.error_message = f"Insufficient tokens. Required: {AGENT_ENTRY_COST}. Mission aborted."
        mission.save()
        return

    # 2. Initialize Agent
    project_vars = {}
    if hasattr(collection.project, 'environment_variables') and collection.project.environment_variables:
        project_vars = collection.project.environment_variables
        
    agent = AutonomousAgent(
        collection, 
        user_story=final_story, 
        env_vars=project_vars,
        scenarios=scenarios,
        categories=categories,
        layer=layer,
        runner_types=runner_types,
        mission_id=mission.id
    )
    
    # 3. Run The Mission (Blocking Call - The Agent thinks and acts)
    try:
        # Increase max_steps for deep multi-scenario testing
        scenario_list = scenarios if isinstance(scenarios, list) else (scenarios.split(',') if isinstance(scenarios, str) else [])
        story_length_bonus = 30 if len(final_story) > 1000 else 0
        mission_depth = min(100, (20 + (len(scenario_list) * 10) + story_length_bonus))
        
        steps_log = agent.run_mission(max_steps=mission_depth)
        logger.info(f"[CollectionAutoPilot] Agent completed {len(steps_log)} steps for {len(scenario_list)} scenarios.")
        
        # 4. Convert Agent Logs to Test Runs (For Dashboard Visibility)
        mission_summary = ""
        for step in steps_log:
            if step['action'] == 'FINISH':
                mission_summary = step.get('reason', '')
                continue

            # Log everything except FINISH
            if step['action'] in ['CALL_API', 'BROWSER_ACTION', 'SHELL_COMMAND', 'MAIL_ACTION']:
                # Find matching endpoint or fallback to first in collection
                endpoint = collection.endpoints.filter(method=step.get('method', 'GET'), url=step.get('url', '')).first()
                if not endpoint and step.get('url'):
                    endpoint = collection.endpoints.filter(url__icontains=step['url']).first()
                
                # If still no endpoint, pick ANY endpoint in the collection as a container
                if not endpoint:
                    endpoint = collection.endpoints.first()
                
                if not endpoint:
                    # If collection is truly empty, we can't create a TestCase (non-nullable endpoint)
                    # For now, skip logging this specific step as a TestRun if collection is empty
                    continue

                # Create a "Record" of what the agent did
                test_case = TestCase.objects.create(
                    endpoint=endpoint,
                    name=f"Step {step['step']}: {step.get('endpoint', step['action'])}",
                    description=f"Action Reason: {step['reason']}",
                    runner_type="browser" if step['action'] == 'BROWSER_ACTION' else "http",
                    category=categories[0] if isinstance(categories, list) and categories else "functional", 
                    layer=layer,
                    tags=[f"SCENARIO:{s}" for s in scenarios.split(',')] if isinstance(scenarios, str) else [f"SCENARIO:{scenarios}"],
                    ai_generated=True,
                    user_story=final_story,
                    assertions=[{"type": "status", "value": step.get('response', {}).get('status', 200)}] if 'response' in step else []
                )
                
                # Build Logs
                req_info = json.dumps(step.get('request', {}), indent=2)
                resp_info = step.get('response', {}).get('body', '') if 'response' in step else ''
                
                final_logs = f"AI THOUGHT: {step['reason']}\n\nACTION TYPE: {step['action']}\n\nREQUEST/DETAILS:\n{req_info}\n\nRESPONSE:\n{resp_info}"
                if mission_summary:
                    final_logs = f"MISSION_SUMMARY: {mission_summary}\n\n" + final_logs

                TestRun.objects.create(
                    test_case=test_case,
                    batch_id=batch_id,
                    status="passed" if step.get('status', 'passed') == "passed" else "failed",
                    response_status=step.get('response', {}).get('status') if 'response' in step else None,
                    response_body=step.get('response', {}).get('body') if 'response' in step else step.get('error'),
                    response_time_ms=step.get('response', {}).get('duration_ms', 0) if 'response' in step else 0,
                    logs=final_logs,
                    triggered_by="ai_agent"
                )
        
        # Populate session report summary
        from django.utils import timezone as tz
        mission.status = "completed"
        mission.completed_at = tz.now()
        mission.total_steps = mission.steps.count()
        mission.passed_steps = mission.steps.filter(status='passed').count()
        mission.failed_steps = mission.steps.filter(status__in=['failed', 'error']).count()
        mission.summary = mission_summary or None
        if mission.created_at:
            delta = tz.now() - mission.created_at
            mission.duration_seconds = int(delta.total_seconds())
        mission.save()
    except Exception as e:
        logger.error(f"[CollectionAutoPilot] Agent Mission Failed: {e}")
        if mission:
            from django.utils import timezone as tz
            mission.status = "error"
            mission.completed_at = tz.now()
            err_str = str(e)
            if '404' in err_str and 'Not Found' in err_str:
                mission.error_message = "The AI model returned an error (404). Check that your API key has access to the configured model."
            elif '429' in err_str:
                mission.error_message = "Rate limited by the AI service. Please wait and try again."
            elif '503' in err_str:
                mission.error_message = "The AI service is temporarily unavailable. Please try again shortly."
            else:
                mission.error_message = f"Mission failed: {err_str[:200]}"
            mission.save()
            
            # Create a failure record in TestRun for visibility
            try:
                from collection.models import Endpoint
                # Ensure at least one endpoint exists
                endpoint = collection.endpoints.first()
                if not endpoint:
                    endpoint = Endpoint.objects.create(
                        collection=collection,
                        name="Agent Target Root",
                        method="GET",
                        url="/"
                    )
                
                test_case = TestCase.objects.create(
                    endpoint=endpoint,
                    name="Critical Agent Failure",
                    description=f"Mission terminated due to error: {str(e)}",
                    ai_generated=True,
                    category="functional"
                )
                TestRun.objects.create(
                    test_case=test_case,
                    batch_id=batch_id,
                    status="error",
                    error_message=str(e),
                    triggered_by="ai_agent"
                )
            except Exception as inner_e:
                logger.error(f"Failed to log mission failure to TestRun: {inner_e}")
        return
    
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
                
                # Use the new Autonomous Agent for the scheduled run
                # This prevents 'bombarding' Redis with many individual tasks
                from .tasks import collection_auto_pilot_task
                collection_auto_pilot_task.delay(
                    collection_id=collection.id,
                    user_id=collection.project.user_id,
                    scenarios="scheduled_smoke_test",
                    batch_id=str(batch_id),
                    user_story="Perform a scheduled smoke test to ensure API stability."
                )
                logger.info(f"Scheduled Agent Mission queued for: {collection.name}")
    
    finally:
        cache.delete(lock_id)


@shared_task(name="run_autonomous_mission", time_limit=3600) # 1 hour max for deep audits
def run_autonomous_mission_task(mission_id, user_id):
    """
    Standardizes the launch of an Autonomous Agent for a specific Mission instance.
    """
    from .models import AgentMission, AgentMissionStep, TestCase, TestRun
    from collection.models import Collection
    from users.models import User
    from .autonomous_agent import AutonomousAgent
    import json
    
    user = User.objects.get(id=user_id)
    mission = AgentMission.objects.get(id=mission_id)
    collection = mission.collection
    
    # 1. Update status
    mission.status = "running"
    mission.save()
    
    from billing.services import deduct_tokens, calculate_test_cost
    
    # Billing check
    entry_cost_key = 'security_audit_entry' if mission.mission_type == "security_audit" else 'agent_mission_entry'
    entry_cost = calculate_test_cost(entry_cost_key)
    
    if not deduct_tokens(user, entry_cost, f"{mission.get_mission_type_display()}: {mission.collection.project.name}"):
        logger.warning(f"Insufficient tokens for mission {mission.id}")
        mission.status = "error"
        mission.error_message = f"Insufficient tokens for {mission.get_mission_type_display()}. Required: {entry_cost}."
        mission.save()
        return

    # 2. Extract context
    project = collection.project
    project_vars = project.environment_variables or {}
    
    # Use user-selected scenarios from the mission (persisted at creation time)
    # Fallback to mission_type defaults only if nothing was stored
    if mission.scenarios:
        scenarios = mission.scenarios if isinstance(mission.scenarios, list) else [mission.scenarios]
    elif mission.mission_type == "security_audit":
        scenarios = ["SECURITY", "AUTH_BYPASS", "INJECTION", "IDOR"]
    else:
        scenarios = ["HAPPY_PATH", "VALIDATION_ERROR", "EDGE_CASE"]
    
    if mission.categories:
        categories = mission.categories
    elif mission.mission_type == "security_audit":
        categories = ["security"]
    else:
        categories = ["functional"]

    # 3. Initialize Agent
    agent = AutonomousAgent(
        collection,
        user_story=mission.user_story,
        env_vars=project_vars,
        scenarios=scenarios,
        categories=categories,
        layer="backend",
        runner_types=["http", "browser"], # default to both for missions
        mission_id=mission.id,
        is_safe_mode=mission.is_safe_mode
    )
    
    # Load browser config if any
    if mission.browser_config:
        agent.browser_config = mission.browser_config

    # 4. Run Mission
    try:
        mission_depth = 40 if mission.mission_type == "security_audit" else 25
        steps_log = agent.run_mission(max_steps=mission_depth)
        
        # 5. Conversion to TestRuns logic (Same as CollectionAutoPilot)
        mission_summary = ""
        for step in steps_log:
            if step['action'] == 'FINISH':
                mission_summary = step.get('reason', '')
                continue
            
            if step['action'] in ['CALL_API', 'BROWSER_ACTION', 'SHELL_COMMAND', 'MAIL_ACTION']:
                endpoint = collection.endpoints.filter(method=step.get('method', 'GET'), url=step.get('url', '')).first()
                if not endpoint and step.get('url'):
                    endpoint = collection.endpoints.filter(url__icontains=step['url']).first()
                
                if not endpoint:
                    endpoint = collection.endpoints.first()
                
                if endpoint:
                    test_case = TestCase.objects.create(
                        endpoint=endpoint,
                        name=f"{mission.get_mission_type_display()} Step: {step.get('endpoint', step['action'])}",
                        description=step['reason'],
                        runner_type="browser" if step['action'] == 'BROWSER_ACTION' else "http",
                        category=categories[0],
                        ai_generated=True,
                        user_story=mission.user_story,
                        assertions=[{"type": "status", "value": step.get('response', {}).get('status', 200)}] if 'response' in step else []
                    )
                    
                    TestRun.objects.create(
                        test_case=test_case,
                        batch_id=mission.batch_id,
                        status="passed" if step.get('status', 'passed') == "passed" else "failed",
                        response_status=step.get('response', {}).get('status') if 'response' in step else None,
                        response_body=step.get('response', {}).get('body') if 'response' in step else step.get('error'),
                        response_time_ms=step.get('response', {}).get('duration_ms', 0) if 'response' in step else 0,
                        logs=f"MISSION TYPE: {mission.mission_type.upper()}\nTHOUGHT: {step['reason']}\n\n{step.get('logs','')}",
                        triggered_by="ai_agent"
                    )
        
        # Populate session report summary
        from django.utils import timezone as tz
        mission.status = "completed"
        mission.completed_at = tz.now()
        mission.total_steps = mission.steps.count()
        mission.passed_steps = mission.steps.filter(status='passed').count()
        mission.failed_steps = mission.steps.filter(status__in=['failed', 'error']).count()
        mission.summary = mission_summary or None
        # Calculate duration from creation time
        if mission.created_at:
            delta = tz.now() - mission.created_at
            mission.duration_seconds = int(delta.total_seconds())
        mission.save()
        
        # Schedule batch report
        from .tasks import send_batch_report_task
        send_batch_report_task.apply_async((str(mission.batch_id), user.id), countdown=60)
        
    except Exception as e:
        logger.error(f"Mission {mission_id} failed: {e}")
        mission.status = "error"
        mission.completed_at = tz.now()
        mission.error_message = str(e)[:200]
        mission.save()
        err_str = str(e)
        if '404' in err_str and 'Not Found' in err_str:
            mission.error_message = "The AI model returned an error (404). Check that your API key has access to the configured model."
        elif '429' in err_str:
            mission.error_message = "Rate limited by the AI service. Please wait and try again."
        elif '503' in err_str:
            mission.error_message = "The AI service is temporarily unavailable. Please try again shortly."
        else:
            mission.error_message = f"Mission failed: {err_str[:200]}"
        mission.save()

        # Create a failure record in TestRun for visibility
        try:
            from collection.models import Endpoint
            endpoint = collection.endpoints.first()
            if not endpoint:
                endpoint = Endpoint.objects.create(
                    collection=collection,
                    name="Agent Target Root",
                    method="GET",
                    url="/"
                )
            
            test_case = TestCase.objects.create(
                endpoint=endpoint,
                name="Critical Mission Crash",
                description=f"Mission terminated: {str(e)}",
                ai_generated=True,
                category="security" if mission.mission_type == "security_audit" else "functional"
            )
            TestRun.objects.create(
                test_case=test_case,
                batch_id=mission.batch_id,
                status="error",
                error_message=str(e),
                triggered_by="ai_agent"
            )
        except Exception as inner_e:
            logger.error(f"Failed to log mission crash to TestRun: {inner_e}")

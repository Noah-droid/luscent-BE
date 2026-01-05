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
def run_test_case_task(self, test_case_id, override_url=None):
    """
    Execute a test case with timeout and retry logic.
    
    Args:
        test_case_id: ID of the test case to run
        override_url: Optional override for the target URL
    
    Raises:
        SoftTimeLimitExceeded: If task exceeds soft_time_limit
        TimeLimitExceeded: If task exceeds time_limit
    """
    try:
        service = RunnerService()
        service.execute_test(test_case_id, override_url=override_url)
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

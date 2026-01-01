import requests
import time
import logging
from .models import TestCase, TestRun
import os


logger = logging.getLogger(__name__)

class RunnerService:
    def execute_test(self, test_case_id):
        try:
            test_case = TestCase.objects.get(id=test_case_id)
        except TestCase.DoesNotExist:
            logger.error(f"TestCase {test_case_id} not found.")
            return None

        # 1. Create Test Run
        test_run = TestRun.objects.create(test_case=test_case, status="running")
        
        try:
            start_time = time.time()
            
            # Dispatch based on type
            runner_type = getattr(test_case, 'runner_type', 'http')
            
            if runner_type == 'http':
                self._run_http_test(test_case, test_run)
            elif runner_type == 'browser':
                self._run_browser_test(test_case, test_run)
            elif runner_type == 'load':
                self._run_load_test(test_case, test_run)
            else:
                raise ValueError(f"Unknown runner type: {runner_type}")
                
            test_run.save()
            
        
            from notifications.services import send_test_run_report
            send_test_run_report(test_run)
            
            return test_run
            
        except Exception as e:
            logger.exception("Runner execution failed")
            test_run.status = "error"
            test_run.error_message = str(e)
            test_run.save()
            

            from notifications.services import send_test_run_report
            send_test_run_report(test_run)
            
            return test_run



    def _run_http_test(self, test_case, test_run):
        """
        Standard internal HTTP execution (using requests)
        Future: Delegate to qai-runner-http container
        """
        from .security import require_safe_url, URLSecurityError
        from django.conf import settings
        
        collection = test_case.collection
        full_url = collection.url
        method = collection.method.upper()
        
        # SECURITY: Validate URL before making request
        try:
            allow_localhost = getattr(settings, 'DEBUG', False)  # Only in development
            require_safe_url(full_url, allow_localhost=allow_localhost)
        except URLSecurityError as e:
            test_run.status = "error"
            test_run.error_message = f"Security Error: {str(e)}"
            logger.warning(f"Blocked unsafe URL in test {test_case.id}: {full_url} - {e}")
            return
        
        # Prepare headers
        headers = {**collection.headers, **test_case.headers}
        
        # Handle Authentication
        if collection.auth_type == "bearer":
             headers["Authorization"] = f"Bearer {collection.auth_value}"
        elif collection.auth_type == "api_key":
             headers["X-API-Key"] = collection.auth_value
        elif collection.auth_type == "basic":
             # Basic auth handling usually requires encoding, skipping for brevity or add logic
             pass 

        params = {**collection.query_params, **test_case.query_params}
        body = test_case.body if test_case.body else collection.request_body
        
        resp = requests.request(
            method=method,
            url=full_url,
            headers=headers,
            params=params,
            json=body,
            timeout=30,
            max_redirects=5,
            allow_redirects=True,
            stream=True,
        )
        
        # SECURITY: Check response size before reading
        MAX_RESPONSE_SIZE = 10 * 1024 * 1024  # 10 MB
        content_length = resp.headers.get('Content-Length')
        
        if content_length:
            try:
                size = int(content_length)
                if size > MAX_RESPONSE_SIZE:
                    test_run.status = "error"
                    test_run.error_message = f"Response too large: {size / (1024*1024):.2f} MB (max: 10 MB)"
                    test_run.response_status = resp.status_code
                    test_run.response_headers = dict(resp.headers)
                    logger.warning(f"Blocked large response for test {test_case.id}: {size} bytes")
                    return
            except ValueError:
                pass  # Invalid Content-Length, proceed with chunk reading
        
        # Read response in chunks with size limit
        content = b""
        try:
            for chunk in resp.iter_content(chunk_size=8192):
                content += chunk
                if len(content) > MAX_RESPONSE_SIZE:
                    test_run.status = "error"
                    test_run.error_message = f"Response exceeded size limit during download (max: 10 MB)"
                    test_run.response_status = resp.status_code
                    test_run.response_headers = dict(resp.headers)
                    logger.warning(f"Response exceeded limit for test {test_case.id}")
                    return
        except Exception as e:
            test_run.status = "error"
            test_run.error_message = f"Error reading response: {str(e)}"
            return
        
        duration = int((time.time() - test_run.executed_at.timestamp()) * 1000)
        passed, error = self._check_assertions(resp, test_case)
        
        test_run.status = "passed" if passed else "failed"
        test_run.error_message = error
        test_run.response_status = resp.status_code
        test_run.response_time_ms = duration
        test_run.response_headers = dict(resp.headers)
        
        # Try to parse as JSON, fallback to text
        try:
            test_run.response_body = resp.json()
        except:
            # Store truncated text response
            try:
                text_content = content.decode('utf-8', errors='ignore')
                test_run.logs = text_content[:5000]  # Truncate to 5000 chars
            except:
                test_run.logs = "Binary response (not displayable)"




    def _run_browser_test(self, test_case, test_run):
        """
        Executes the generated Playwright script in a subprocess.
        WARNING: This is running generated code on the host machine. 
        ENSURE PROPER ISOLATION IN PRODUCTION (Docker/Firecracker).
        """
        script_content = test_case.test_script
        if not script_content:
            test_run.status = "failed"
            test_run.error_message = "No test script provided for browser test."
            return

        import subprocess
        import sys
        import tempfile
        import os
        from django.core.files import File

        # Prepare screenshot path (temp dir)
        screenshot_dir = tempfile.mkdtemp()
        screenshot_path = os.path.join(screenshot_dir, "screenshot.png")

        # Write script to temp file
        with tempfile.NamedTemporaryFile(mode='w', suffix='.py', delete=False) as tmp:
            # Prepend necessary imports if missing (simple heuristic)
            if "from playwright.sync_api" not in script_content:
                tmp.write("from playwright.sync_api import sync_playwright\n")
            
            # Wrap in a run function if it looks like a snippet
            tmp.write(script_content)
            tmp_path = tmp.name

        try:
            # Execute with RESTRICTED environment to prevent leaking secrets
            # We only pass essential vars (PATH, etc.)
            clean_env = {
                "PATH": os.environ.get("PATH", ""),
                "SYSTEMROOT": os.environ.get("SYSTEMROOT", ""), # Windows
                "HOME": os.environ.get("HOME", ""),
                "LANG": "en_US.UTF-8",
                "SCREENSHOT_PATH": screenshot_path # Pass to script
            }
            
            start = time.time()
            process = subprocess.run(
                [sys.executable, tmp_path],
                env=clean_env, 
                capture_output=True,
                text=True,
                timeout=60 # 60s timeout
            )
            duration = int((time.time() - start) * 1000)
            
            test_run.logs = f"STDOUT:\n{process.stdout}\n\nSTDERR:\n{process.stderr}"
            test_run.response_time_ms = duration
            
            # Check for screenshot artifact and upload to Cloudinary
            if os.path.exists(screenshot_path):
                from .storage import CloudinaryStorage
                
                try:
                    storage = CloudinaryStorage()
                    project = test_case.collection.project
                    
                    result = storage.upload_screenshot(
                        file_path=screenshot_path,
                        project=project,
                        test_run_id=test_run.id
                    )
                    
                    # Save Cloudinary URL and metadata
                    test_run.screenshot_url = result['url']
                    test_run.screenshot_public_id = result['public_id']
                    test_run.screenshot_size_bytes = result['size_bytes']
                    
                    logger.info(f"Screenshot uploaded: {result['url']} ({result['size_bytes']} bytes)")
                    
                except ValueError as e:
                    # Quota exceeded or upload failed
                    test_run.logs += f"\n[Storage Error] {str(e)}"
                    logger.warning(f"Screenshot upload failed for run {test_run.id}: {e}")
                except Exception as e:
                    test_run.logs += f"\n[Storage Error] Unexpected error: {str(e)}"
                    logger.error(f"Unexpected error uploading screenshot: {e}")
            
            if process.returncode == 0:
                test_run.status = "passed"
                
                # Visual AI Check (Optional)
                if getattr(test_case, 'use_visual_ai', False) and test_run.screenshot_url:
                    self._perform_visual_ai_check(test_run, screenshot_path)
                    
            else:
                test_run.status = "failed"
                test_run.error_message = f"Script exited with code {process.returncode}"

        except subprocess.TimeoutExpired:
            test_run.status = "failed"
            test_run.error_message = "Test execution timed out (60s)."
        except Exception as e:
            test_run.status = "error"
            test_run.error_message = f"Execution error: {str(e)}"
        finally:
            # Cleanup
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
            if os.path.exists(screenshot_path):
                os.remove(screenshot_path)
            try:
                os.rmdir(screenshot_dir)
            except:
                pass


    def _perform_visual_ai_check(self, test_run, screenshot_path):
        """
        Sends screenshot to Vision Model for analysis.
        Updates test_run.status if visual defects found.
        
        Args:
            test_run: TestRun instance
            screenshot_path: Path to the screenshot file on disk
        """
        from .ai_generator import AITestGenerator
        
        try:
            if not screenshot_path or not os.path.exists(screenshot_path):
                return

            test_run.logs += "\n[Visual AI] Analysis requested..."
            test_run.save()
            
            generator = AITestGenerator()
            result = generator.analyze_screenshot(screenshot_path)
            
            test_run.logs += f"\n[Visual AI] Result: {result}"
            
            # If defects found, we might want to flag the run
            if "NO_DEFECTS" not in result and "Error" not in result:
                
                 test_run.error_message = f"{test_run.error_message or ''}\n[Visual Defect]: {result}".strip()
                 
            
            test_run.save()

        except Exception as e:
            test_run.logs += f"\n[Visual AI] Error: {e}"
            test_run.save()


    def _run_load_test(self, test_case, test_run):
        """
        Executes a Locust load test against the target endpoint.
        """
        import subprocess
        import json
        import shutil
        
        # Check if locust is installed
        if not shutil.which("locust"):
            test_run.status = "error" 
            test_run.error_message = "Locust not installed on server."
            test_run.save()
            return

        # Prepare payload for the locustfile
        endpoint_data = {
            "method": test_case.collection.method,
            "url": test_case.collection.url, # Full URL
            "headers": {**test_case.collection.headers, **test_case.headers},
            "body": test_case.body or test_case.collection.request_body,
            "expected_status": test_case.expected_status
        }
        
        # Path to our generic locustfile
        locust_file = os.path.join(os.path.dirname(__file__), "locustfile.py")
        
        # Extract Base Host (Scheme + Netloc)
        from urllib.parse import urlparse
        parsed = urlparse(test_case.collection.url)
        base_host = f"{parsed.scheme}://{parsed.netloc}"

        # Command: locust -f locustfile.py --headless -u 10 -r 2 -t 10s --host=... --endpoint-data='{...}'
        cmd = [
            "locust", 
            "-f", locust_file,
            "--headless",
            "--users", "5",
            "--spawn-rate", "1",
            "--run-time", "10s",
            "--host", base_host, 
            "--endpoint-data", json.dumps(endpoint_data),
            "--csv", "locust_result", 
        ]
        
        try:
            start = time.time()
            process = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                cwd="/tmp", # Run in temp to avoid cluttering project root with logs
                timeout=60
            ) 
            duration = int((time.time() - start) * 1000)
            
            # Locust outputs stats to stderr usually
            logs = f"STDOUT:\n{process.stdout}\n\nSTDERR:\n{process.stderr}"
            
            test_run.logs = logs
            test_run.response_time_ms = duration
            
            if process.returncode == 0:
                 test_run.status = "passed"
                 test_run.response_status = 200 # Placeholder for "Test Finished"
            else:
                 test_run.status = "failed"
                 test_run.error_message = "Locust exited with error."
                 
        except subprocess.TimeoutExpired:
            test_run.status = "failed" 
            test_run.error_message = "Load test timed out."
        except Exception as e:
            test_run.status = "error"
            test_run.error_message = f"Load runner failed: {e}"

    def _check_assertions(self, response, test_case):
        if response.status_code != test_case.expected_status:
            return False, f"Status mismatch: Expected {test_case.expected_status}, got {response.status_code}"
            
        for assertion in test_case.assertions:
            atype = assertion.get("type")
            if atype == "status":
                expected = assertion.get("value")
                if response.status_code != int(expected):
                    return False, f"Status assertion failed: Expected {expected}, got {response.status_code}"
        return True, None




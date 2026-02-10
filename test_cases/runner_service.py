import requests
import time
import logging
from .models import TestCase, TestRun
import os
import shutil
import subprocess
import tempfile
import json
from django.conf import settings


logger = logging.getLogger(__name__)

class RunnerService:
    # runner image
    RUNNER_IMAGE = "qai-runner-python:latest"

    def execute_test(self, test_case_id, override_url=None, batch_id=None, triggered_by="manual"):
        logger.info(f"[RunnerService] Starting test execution for test_case_id={test_case_id}, batch_id={batch_id}, triggered_by={triggered_by}")
        try:
            test_case = TestCase.objects.get(id=test_case_id)
            logger.info(f"[RunnerService] Found test case: {test_case.name} (runner_type={test_case.runner_type})")
        except TestCase.DoesNotExist:
            logger.error(f"TestCase {test_case_id} not found.")
            return None

        # Create Test Run
        test_run = TestRun.objects.create(
            test_case=test_case, 
            status="running",
            batch_id=batch_id,
            triggered_by=triggered_by,
        )
        
        try:
            start_time = time.time()
            
            # Dispatch based on type
            runner_type = getattr(test_case, 'runner_type', 'http')
            
            if runner_type == 'http':
                self._run_http_test(test_case, test_run, override_url=override_url)
            elif runner_type == 'browser':
                self._run_browser_test(test_case, test_run, override_url=override_url)
            elif runner_type == 'load':
                self._run_load_test(test_case, test_run, override_url=override_url)
            else:
                raise ValueError(f"Unknown runner type: {runner_type}")
                
            test_run.save()
            
            # Increment lifetime stat
            try:
                test_case.endpoint.collection.project.user.increment_test_runs()
            except Exception as e:
                logger.error(f"Failed to increment test_runs_count: {e}")
            
        
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



    def _run_http_test(self, test_case, test_run, override_url=None):
        """
        Executes an HTTP request inside the hardened sandbox.
        """
        from .security import require_safe_url, URLSecurityError
        from django.conf import settings
        
        endpoint = test_case.endpoint
        
        # URL Resolution
        if override_url:
            from urllib.parse import urlparse
            orig_parsed = urlparse(endpoint.url)
            full_url = endpoint.url.replace(f"{orig_parsed.scheme}://{orig_parsed.netloc}", override_url.rstrip('/'))
        else:
            full_url = endpoint.url

        # SECURITY: Validate URL on host before sending to sandbox
        try:
            allow_localhost = getattr(settings, 'DEBUG', False)
            require_safe_url(full_url, allow_localhost=allow_localhost)
        except URLSecurityError as e:
            test_run.status = "error"
            test_run.error_message = f"Security Error: {str(e)}"
            test_run.save()
            return
        
        # Prepare data for the sandbox script
        headers = {**endpoint.headers, **test_case.headers}
        if endpoint.auth_type == "bearer":
             headers["Authorization"] = f"Bearer {endpoint.auth_value}"
        elif endpoint.auth_type == "api_key":
             headers["X-API-Key"] = endpoint.auth_value

        params = {**endpoint.query_params, **test_case.query_params}
        body = test_case.body if test_case.body else endpoint.request_body
        
        # Generate the execution script
        # JSON serialization 
        sandbox_script = f"""
        import requests
        import json
        import time

        try:
            start_time = time.time()
            resp = requests.request(
                method="{endpoint.method.upper()}",
                url="{full_url}",
                headers={json.dumps(headers)},
                params={json.dumps(params)},
                json={json.dumps(body)},
                timeout=30,
                allow_redirects=True,
            )
            duration = int((time.time() - start_time) * 1000)
            
            # Check size limit in container too (defense in depth)
            MAX_SIZE = 10 * 1024 * 1024
            content = resp.content
            if len(content) > MAX_SIZE:
                print(json.dumps({{"error": "Response exceeded 10MB limit"}}))
                exit(0)

            # Output structured results
            print(json.dumps({{
                "status": resp.status_code,
                "headers": dict(resp.headers),
                "body": resp.text if len(content) < 1000000 else "Body too large to display",
                "duration": duration
            }}))
        except Exception as e:
            print(json.dumps({{"error": str(e)}}))
        """

        # Run in Sandbox
        result = self._run_in_sandbox(sandbox_script, timeout=40)
        
        # Parse result and update models
        if "error" in result:
             test_run.status = "error"
             test_run.error_message = result["error"]
             test_run.save()
             return

        try:
            # The script outputs a single JSON line
            stdout_data = result.get("stdout", "").strip()
            # If there's multiple lines (from prints), take the last one
            last_line = stdout_data.split('\n')[-1]
            http_data = json.loads(last_line)
            
            if "error" in http_data:
                test_run.status = "failed"
                test_run.error_message = http_data["error"]
            else:
                # Use the dummy response object to check assertions
                from collections import namedtuple
                ResponseMock = namedtuple('ResponseMock', ['status_code', 'text', 'headers'])
                mock_resp = ResponseMock(
                    status_code=http_data['status'],
                    text=http_data['body'],
                    headers=http_data['headers']
                )
                
                passed, error = self._check_assertions(mock_resp, test_case)
                
                test_run.status = "passed" if passed else "failed"
                test_run.error_message = error
                test_run.response_status = http_data['status']
                test_run.response_headers = http_data['headers']
                test_run.response_body = http_data['body']
                test_run.response_time_ms = http_data['duration']
                
        except Exception as e:
            test_run.status = "error"
            test_run.error_message = f"Failed to parse sandbox output: {str(e)}"
            test_run.logs = result.get("stdout")
        
        test_run.save()




    def _run_in_sandbox(self, script_content, env_vars=None, timeout=60, use_network=True):
        """
        Executes a script inside a hardened sandbox.
        Automatically switches between Local Docker and Remote Cloud Run.
        """
        # Check for Remote Runner (Production Mode)
        remote_url = getattr(settings, 'QAI_RUNNER_URL', None)
        if remote_url:
            logger.info(f"[RunnerService] Using REMOTE runner at: {remote_url}")
            return self._run_remote(remote_url, script_content, env_vars, timeout)

        # Check for Local Docker (Development Mode)
        logger.info("[RunnerService] Remote runner not configured, checking for local Docker...")
        if not shutil.which("docker"):
            logger.warning("Docker not found. Falling back to host execution (UNSAFE).")
            return self._run_on_host(script_content, env_vars, timeout)

        # Prepare temp directory for script and results
        temp_dir = tempfile.mkdtemp()
        script_path = os.path.join(temp_dir, "test_script.py")
        
        with open(script_path, "w") as f:
            f.write(script_content)

        try:
            # Build docker command
            cmd = [
                "docker", "run", "--rm",
                "--network", "bridge" if use_network else "none",
                "--memory", "512m",
                "--cpus", "0.5",
                "-v", f"{temp_dir}:/app",
                "-w", "/app",
            ]

            # Add gVisor runtime if enabled in settings
            gvisor_runtime = getattr(settings, 'DOCKER_RUNTIME_SECURE', None)
            if gvisor_runtime:
                cmd.extend(["--runtime", gvisor_runtime])

            # Add environment variables
            if env_vars:
                for k, v in env_vars.items():
                    cmd.extend(["-e", f"{k}={v}"])

            # Add image and script to execute
            # We must explicitly call 'python' because the Dockerfile default is now the API server
            cmd.extend([self.RUNNER_IMAGE, "python3", "test_script.py"])

            start_time = time.time()
            process = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout
            )
            duration = int((time.time() - start_time) * 1000)

            return {
                "stdout": process.stdout,
                "stderr": process.stderr,
                "returncode": process.returncode,
                "duration": duration,
                "temp_dir": temp_dir # Kept for artifact extraction
            }

        except subprocess.TimeoutExpired:
            return {"error": f"Timeout after {timeout}s", "returncode": -1, "duration": timeout*1000}
        except Exception as e:
            return {"error": str(e), "returncode": -1, "duration": 0}

    def _run_remote(self, url, script_content, env_vars=None, timeout=60):
        """
        Sends the test to a remote Cloud Run instance.
        """
        secret = getattr(settings, 'QAI_RUNNER_SECRET', "")
        
        logger.info(f"[RemoteRunner] Sending request to {url}/execute with timeout={timeout}s")
        
        try:
            start_time = time.time()
            response = requests.post(
                f"{url.rstrip('/')}/execute",
                json={
                    "script": script_content,
                    "env_vars": env_vars or {},
                    "timeout": timeout
                },
                headers={"X-QAi-Secret": secret},
                timeout=timeout + 5
            )
            
            logger.info(f"[RemoteRunner] Response status: {response.status_code}")
            
            if response.status_code != 200:
                error_msg = f"Remote runner error: {response.text}"
                logger.error(f"[RemoteRunner] {error_msg}")
                return {"error": error_msg, "returncode": -1}

            data = response.json()
            logger.info(f"[RemoteRunner] Execution completed successfully in {int((time.time() - start_time) * 1000)}ms")
            
            # If there's a screenshot, we need to save it locally for artifact processing
            temp_dir = None
            if data.get("screenshot_b64"):
                import base64
                temp_dir = tempfile.mkdtemp()
                with open(os.path.join(temp_dir, "screenshot.png"), "wb") as f:
                    f.write(base64.b64decode(data["screenshot_b64"]))
            
            return {
                "stdout": data.get("stdout", ""),
                "stderr": data.get("stderr", ""),
                "returncode": data.get("returncode", 0),
                "duration": data.get("duration", int((time.time() - start_time) * 1000)),
                "temp_dir": temp_dir
            }

        except requests.exceptions.Timeout as e:
            error_msg = f"Remote connection timeout after {timeout}s: {str(e)}"
            logger.error(f"[RemoteRunner] {error_msg}")
            return {"error": error_msg, "returncode": -1}
        except requests.exceptions.RequestException as e:
            error_msg = f"Remote connection failed: {str(e)}"
            logger.error(f"[RemoteRunner] {error_msg}")
            return {"error": error_msg, "returncode": -1}
        except Exception as e:
            error_msg = f"Unexpected error: {str(e)}"
            logger.error(f"[RemoteRunner] {error_msg}")
            return {"error": error_msg, "returncode": -1}

    def _run_on_host(self, script_content, env_vars=None, timeout=60):
        """Standard subprocess execution (Original logic)"""
        with tempfile.NamedTemporaryFile(mode='w', suffix='.py', delete=False) as tmp:
            tmp.write(script_content)
            tmp_path = tmp.name

        try:
            start = time.time()
            process = subprocess.run(
                ["python3", tmp_path],
                env={**(env_vars or {}), "PATH": os.environ.get("PATH", "")},
                capture_output=True,
                text=True,
                timeout=timeout
            )
            return {
                "stdout": process.stdout,
                "stderr": process.stderr,
                "returncode": process.returncode,
                "duration": int((time.time() - start) * 1000)
            }
        finally:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)

    def _run_browser_test(self, test_case, test_run, override_url=None):
        """
        Executes the generated Playwright script in a sandbox.
        """
        script_content = test_case.test_script
        if not script_content:
            test_run.status = "failed"
            test_run.error_message = "No test script provided for browser test."
            return

        # Ensure necessary imports
        if "from playwright.sync_api" not in script_content:
            script_content = "from playwright.sync_api import sync_playwright\n" + script_content

        # Run in sandbox
        result = self._run_in_sandbox(
            script_content,
            env_vars={"SCREENSHOT_PATH": "/app/screenshot.png"},
            timeout=60
        )

        test_run.logs = f"STDOUT:\n{result.get('stdout', '')}\n\nSTDERR:\n{result.get('stderr', '')}"
        if "error" in result:
             test_run.error_message = result["error"]
             test_run.status = "error"
             test_run.save()
             return

        test_run.response_time_ms = result["duration"]
        temp_dir = result.get("temp_dir")
        screenshot_path = os.path.join(temp_dir, "screenshot.png") if temp_dir else None

        try:
            # Check for screenshot artifact
            if screenshot_path and os.path.exists(screenshot_path):
                from .storage import CloudinaryStorage
                try:
                    storage = CloudinaryStorage()
                    upload_res = storage.upload_screenshot(
                        file_path=screenshot_path,
                        project=test_case.endpoint.collection.project,
                        test_run_id=test_run.id
                    )
                    test_run.screenshot_url = upload_res['url']
                    test_run.screenshot_public_id = upload_res['public_id']
                except Exception as e:
                    test_run.logs += f"\n[Storage Error] {str(e)}"

            if result["returncode"] == 0:
                test_run.status = "passed"
                if getattr(test_case, 'use_visual_ai', False) and test_run.screenshot_url:
                    self._perform_visual_ai_check(test_run, screenshot_path)
            else:
                test_run.status = "failed"
                test_run.error_message = f"Exit code: {result['returncode']}"

        finally:
            # Cleanup temp dir
            if temp_dir and os.path.exists(temp_dir):
                shutil.rmtree(temp_dir)
            test_run.save()


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


    def _run_load_test(self, test_case, test_run, override_url=None):
        """
        Executes a Locust load test inside the sandbox.
        Uses the locustfile.py template from the local directory.
        """
        # Prepare endpoint data
        current_url = test_case.endpoint.url
        if override_url:
            from urllib.parse import urlparse
            orig_parsed = urlparse(current_url)
            current_url = current_url.replace(f"{orig_parsed.scheme}://{orig_parsed.netloc}", override_url.rstrip('/'))

        endpoint_data = {
            "method": test_case.endpoint.method,
            "url": current_url,
            "headers": {**test_case.endpoint.headers, **test_case.headers},
            "body": test_case.body or test_case.endpoint.request_body,
            "expected_status": test_case.expected_status
        }
        
        # Load the locustfile template from disk
        locustfile_path = os.path.join(os.path.dirname(__file__), "locustfile.py")
        try:
            with open(locustfile_path, "r") as f:
                locust_script_template = f.read()
        except Exception as e:
            logger.error(f"Failed to read locustfile.py template: {e}")
            # Fallback to minimal safety script
            locust_script_template = "from locust import HttpUser, task\nclass User(HttpUser):\n  @task\n  def t(self): pass"

        # Simpler: Just use our sandbox to run a script that calls locust via subprocess
        # We pass endpoint_data via CLI as the locustfile expects
        endpoint_data_json = json.dumps(endpoint_data)
        
        wrapper_script = f"""
import subprocess
import sys
import os
import json

# Write the template to the sandbox
script_content = {repr(locust_script_template)}
with open("locustfile.py", "w") as f:
    f.write(script_content)

endpoint_data_str = {repr(endpoint_data_json)}

# Run locust with the custom argument defined in locustfile.py
res = subprocess.run([
    "locust", 
    "-f", "locustfile.py", 
    "--headless", 
    "-u", "5", 
    "-r", "1", 
    "-t", "10s",
    "--endpoint-data", endpoint_data_str
], capture_output=True, text=True)

print(res.stdout)
print(res.stderr, file=sys.stderr)
sys.exit(res.returncode)
"""

        result = self._run_in_sandbox(wrapper_script, timeout=30)
        
        test_run.logs = f"STDOUT:\n{result.get('stdout', '')}\n\nSTDERR:\n{result.get('stderr', '')}"
        test_run.response_time_ms = result.get("duration", 0)
        
        if result.get("returncode") == 0:
            test_run.status = "passed"
            test_run.response_status = 200
        else:
            test_run.status = "failed"
            test_run.error_message = result.get("error", "Locust execution failed")
        
        test_run.save()

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




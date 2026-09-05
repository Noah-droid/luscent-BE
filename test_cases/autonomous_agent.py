import logging
import os
import time
import re
import json
import requests
from django.conf import settings
import litellm
from .models import AgentMission, AgentMissionStep, AgentPrompt

try:
    from e2b import Sandbox
except ImportError:
    Sandbox = None

logger = logging.getLogger(__name__)



class AutonomousAgent:
    """
    A Self-Driving QA Agent that executes API tests live, reacts to errors,
    and maintains state like a human tester.
    """
    def __init__(self, collection, user_story=None, env_vars=None, scenarios=None, categories=None, layer="backend", runner_types=None, mission_id=None, is_safe_mode=True):
        self.collection = collection
        self.mission_id = mission_id
        self.is_safe_mode = is_safe_mode
        self.user_story = user_story or "Explore the API and ensure core functionality works."
        self.env_vars = env_vars or {}
        self.browser_process = None # Persistent browser process for live view
        self.previous_failures = [] # Populated for regression missions
        
        # Mission Context (Determines the agent's focus)
        self.scenarios = scenarios or "HAPPY_PATH"
        self.categories = categories or ["functional"]
        self.layer = layer
        self.runner_types = runner_types
        if not self.runner_types:
            if self.collection.source in ['browser', 'crawler']:
                self.runner_types = ["http", "browser"]
            else:
                self.runner_types = ["http"]
        
        # LLM Config
        self.provider = getattr(settings, 'LLM_PROVIDER', 'gemini').lower() 
        self.openai_api_key = getattr(settings, 'LLM_API_KEY', None)
        self.gemini_api_key = getattr(settings, 'GEMINI_API_KEY', None)
        self.nvidia_api_key = getattr(settings, 'NVIDIA_NIM_API_KEY', None)
        self.e2b_api_key = getattr(settings, 'E2B_API_KEY', None)
        self.sandbox_template = getattr(settings, 'E2B_SANDBOX_TEMPLATE', 'qai-runner')
        
        # litellm model naming: prefix provider for auto-routing
        if self.provider == "gemini":
            raw_model = getattr(settings, 'GEMINI_MODEL', 'gemini-3.5-flash')
            self.model = raw_model if raw_model.startswith('gemini/') else f'gemini/{raw_model}'
        elif self.provider == "nvidia":
            raw_model = getattr(settings, 'NVIDIA_MODEL', 'nvidia/nemotron-3-nano-omni-30b-a3b-reasoning')
            self.model = raw_model if raw_model.startswith('nvidia_nim/') else f'nvidia_nim/{raw_model}'
        else:
            raw_model = getattr(settings, 'OPENAI_MODEL', 'gpt-4o-mini')
            self.model = raw_model  # OpenAI models don't need a prefix
        
        # Propagate API keys to env for litellm auto-detection
        if self.gemini_api_key:
            os.environ['GEMINI_API_KEY'] = self.gemini_api_key
        if self.openai_api_key:
            os.environ['OPENAI_API_KEY'] = self.openai_api_key
        if self.nvidia_api_key:
            os.environ['NVIDIA_NIM_API_KEY'] = self.nvidia_api_key
            os.environ['NVIDIA_API_KEY'] = self.nvidia_api_key

        # Agent Memory
        self.history = [] 
        self.extracted_vars = {} 
        self.sandbox = None # Initialized during mission
        self.browser_config = {} # Initialized from mission context
        self.mail_session = None # Mail.tm session
        self.current_email = None
        
        if self.env_vars:
            self.extracted_vars.update(self.env_vars)

        # Internal state for browser manager
        self.browser_process = None
        self._browser_manager_failed = False
        self.previous_failures = []

    def _sandbox_file_write(self, path, content, retries=3):
        """Write a file to the sandbox with retry logic for 502/transient errors."""
        for attempt in range(retries):
            try:
                self.sandbox.files.write(path, content)
                return True
            except Exception as e:
                logger.warning(f"[Agent] File write attempt {attempt + 1} failed for {path}: {e}")
                if attempt < retries - 1:
                    time.sleep(2)
                    # Check if sandbox is still alive
                    try:
                        self.sandbox.commands.run("echo ok", timeout=5)
                    except Exception:
                        logger.error(f"[Agent] Sandbox appears dead, cannot write {path}")
                        return False
        return False

    def _sandbox_health_check(self):
        """Quick check that the sandbox is responsive."""
        try:
            result = self.sandbox.commands.run("echo healthy", timeout=5)
            return result.exit_code == 0
        except Exception:
            return False

    def run_mission(self, max_steps=10):
        """
        Executes the agent loop until the user story is satisfied or max_steps reached.
        Returns a list of steps performed with results.
        """
        logger.info(f"[Agent] Starting mission for {self.collection.name}: {self.user_story}")
        
        mission = None
        if self.mission_id:
            try:
                mission = AgentMission.objects.get(id=self.mission_id)
                mission.status = "running"
                mission.save()
                self.browser_config = mission.browser_config or {}
                
                # LOAD REGRESSION CONTEXT: If previous_session is linked,
                # pull the failed steps so the agent knows exactly what to re-test
                if mission.previous_session:
                    prev = mission.previous_session
                    failed_steps = prev.steps.filter(status='failed').values(
                        'step_number', 'action_type', 'thought', 'response_status', 'response_body'
                    )
                    self.previous_failures = list(failed_steps)
                    logger.info(f"[Agent] Loaded {len(self.previous_failures)} past failures from session {prev.batch_id}")
            except AgentMission.DoesNotExist:
                logger.warning(f"[Agent] Mission ID {self.mission_id} not found.")

        # 1. Understudy: Initialize Context
        # We now include the schema and ensure IDs are strings for the JSON prompt.
        endpoints_raw = list(self.collection.endpoints.values(
            'id', 'method', 'url', 'name', 'description', 
            'request_body', 'query_params', 'auth_type', 'headers'
        ))
        
        # Format for AI: Convert UUIDs to strings and clean up
        endpoints = []
        for e in endpoints_raw:
            e_fixed = e.copy()
            e_fixed['id'] = str(e['id'])
            endpoints.append(e_fixed)
            
        endpoint_map = {str(e['id']): e for e in endpoints_raw}
        
        logger.info(f"[Agent] Discovery complete. Map size: {len(endpoints)} endpoints.")
        
        # 2. START SANDBOX (The Body)
        if not Sandbox or not self.e2b_api_key:
            logger.error("[Agent] E2B not configured. Switching to HOST-ONLY mode (Unsafe/Limited).")
        else:
            try:
                logger.info(f"[Agent] Spawning sandbox environment: {self.sandbox_template}")
                # Retry sandbox creation up to 3 times — E2B can transient-fail
                self.sandbox = None
                for _sandbox_attempt in range(3):
                    try:
                        self.sandbox = Sandbox.create(
                            template=self.sandbox_template,
                            api_key=self.e2b_api_key,
                            timeout=900  # 15 minutes — long enough for QA missions with VNC
                        )
                        break
                    except Exception as se:
                        logger.warning(f"[Agent] Sandbox create attempt {_sandbox_attempt + 1} failed: {se}")
                        if _sandbox_attempt < 2:
                            time.sleep(3)
                        else:
                            raise

                # Extend timeout during execution — QA missions can take a while
                try:
                    self.sandbox.set_timeout(900)
                except Exception:
                    pass  # Not all SDK versions support this
                
                # Store mission reference and initialise URL holders
                self._current_mission = mission
                self._vnc_url = None   # set by _start_gui_stack
                self._app_url = None   # set by _execute_whitebox_setup

                # If we have a repo linked, clone + start the app FIRST so
                # port 80 isn't taken by the app before VNC claims it.
                if mission and mission.collection.project.repo_url:
                    self._execute_whitebox_setup(mission.collection.project, mission)

                # Start GUI stack (Xvfb + VNC) for live view — runs AFTER
                # whitebox so the VNC URL is the one persisted to session_url
                # (used by the frontend iframe for live visual monitoring).
                self._start_gui_stack()

                # Persist URLs to the mission model
                if mission:
                    # session_url → VNC live view (iframe)
                    if self._vnc_url:
                        mission.session_url = self._vnc_url
                    # If no VNC fell back to the app URL
                    elif self._app_url:
                        mission.session_url = self._app_url
                    # app_url → direct link to the user's running app
                    if self._app_url:
                        mission.app_url = self._app_url
                    mission.save()
                    logger.info(f"[Agent] Mission session_url={mission.session_url}  app_url={mission.app_url}")

                # Health check before browser init — sandbox may have died during GUI stack setup
                if "browser" in self.runner_types:
                    if self._sandbox_health_check():
                        self._init_browser_manager()
                    else:
                        logger.error("[Agent] Sandbox unhealthy after GUI stack — skipping browser manager")
                
            except Exception as e:
                logger.error(f"[Agent] Failed to spawn sandbox: {e}")

        system_prompt = self._build_system_prompt(endpoints)
        self.history.append({"role": "system", "content": system_prompt})
        
        # Playwright verification state
        self._playwright_verified = False

        steps_log = []
        correction_count = 0

        try:
            # 3. The Loop
            for step_i in range(1, max_steps + 1):
                # 3a. Check for user prompts (Live Interaction) + pause/resume handling
                if mission:
                    # RELOAD mission status to detect pause/resume from UI
                    mission.refresh_from_db()
                    
                    # If paused by human takeover, spin-wait until resumed
                    while mission.status == 'paused':
                        logger.info(f"[Agent] Mission paused — waiting for human takeover to resume...")
                        time.sleep(5)
                        mission.refresh_from_db()
                        if mission.status == 'error':
                            logger.info("[Agent] Mission was killed while paused.")
                            return steps_log                    # Check for new prompts (including takeover signals)
                    new_prompts = AgentPrompt.objects.filter(mission=mission, is_processed=False).order_by('created_at')
                    for p in new_prompts:
                        logger.info(f"[Agent] Received User Guidance: {p.prompt}")
                        self.history.append({"role": "user", "content": f"USER GUIDANCE / INSTRUCTION: {p.prompt}"})
                        p.is_processed = True
                        p.save()

                    # Check if mission was stopped by user
                    if mission.status == 'error':
                        logger.info("[Agent] Mission was stopped by user.")
                        return steps_log

                logger.info(f"--- [Step {step_i}/{max_steps}] Thinking ---")
                
                action = self._get_next_action()
                reason = action.get('reason', 'No reason provided')
                action_type = action.get('type', 'UNKNOWN')
                logger.info(f"[Agent Decision] Type: {action_type} | Reason: {reason}")
                
                # SCENARIO ENFORCEMENT: If agent strays from selected scenarios, redirect it
                if self.scenarios and action_type != 'FINISH' and action_type != 'ERROR':
                    selected = [s.upper().replace(' ', '_').replace('-', '_') for s in (self.scenarios if isinstance(self.scenarios, list) else [self.scenarios])]
                    reason_upper = (reason + ' ' + str(action.get('details', {}))).upper()
                    # Check if reason mentions a scenario NOT in the selected list
                    ALL_SCENARIOS = {'HAPPY_PATH', 'SECURITY', 'VALIDATION_ERROR', 'EDGE_CASE', 'PERFORMANCE', 'SMOKE', 'REGRESSION', 'E2E'}
                    mentioned = ALL_SCENARIOS & set(re.findall(r'\b(' + '|'.join(ALL_SCENARIOS) + r')\b', reason_upper))
                    off_topic = mentioned - set(selected)
                    if off_topic:
                        logger.warning(f"[Agent] Scenario drift detected: {off_topic}. Redirecting to selected: {selected}")
                        self._record_observation(
                            f"IMPORTANT: You are straying from the assigned scenarios. "
                            f"Your assigned scenarios are ONLY: {', '.join(selected)}. "
                            f"You mentioned {', '.join(off_topic)} which are NOT assigned. "
                            f"Please refocus ONLY on: {', '.join(selected)}."
                        )
                        correction_count += 1
                        if correction_count >= 3:
                            # After 3 drifts, force FINISH to prevent wasted tokens
                            logger.info("[Agent] Too many scenario drifts — forcing mission completion.")
                            action = {"type": "FINISH", "reason": f"Completed assigned scenarios: {', '.join(selected)}"}
                        else:
                            continue
                
                if action.get("type") == "FINISH":
                    logger.info(f"[Agent] Mission Complete: {action.get('reason')}")
                    steps_log.append({
                        "step": step_i, "action": "FINISH", "reason": action.get("reason"),
                        "self_correction_count": correction_count, "status": "success"
                    })
                    if mission:
                        mission.status = "completed"
                        mission.save()
                        
                        AgentMissionStep.objects.create(
                            mission=mission,
                            step_number=step_i,
                            action_type="FINISH",
                            thought=action.get("reason"),
                            status="passed"
                        )
                    break
                    
                if action.get("type") == "ERROR":
                    correction_count += 1
                    self._record_observation(f"Error from Brain: {action.get('reason')}. Retrying.")
                    continue

                # DISPATCH TO SANDBOX (with Hybrid Billing)
                from billing.services import deduct_tokens, calculate_test_cost
                
                # Map action to billing type
                billing_map = {
                    "CALL_API": "http",
                    "BROWSER_ACTION": "browser",
                    "STRESS_TEST": "load",
                    "SHELL_COMMAND": "shell_command",
                    "MAIL_ACTION": "mail_action"
                }
                
                cost_key = billing_map.get(action_type, "http")
                step_cost = calculate_test_cost(cost_key)
                
                # Deduct tokens for the step
                if mission:
                    user = mission.user
                    success = deduct_tokens(user, step_cost, f"Agent Step {step_i}: {action_type}", ref_id=mission.id)
                    if not success:
                        logger.error(f"[Agent] Insufficient tokens for step {step_i}. Mission aborted.")
                        mission.status = "error"
                        mission.error_message = f"Insufficient tokens for Step {step_i} ({action_type}). Cost: {step_cost}. Balance too low."
                        mission.save()
                        self._record_observation("Error: Out of tokens. Mission ending.")
                        break

                result = None
                if action_type == "CALL_API":
                    result = self._execute_api_call(action, endpoint_map)
                elif action_type == "BROWSER_ACTION":
                    result = self._execute_browser_action(action)
                elif action_type == "STRESS_TEST":
                    result = self._execute_stress_test(action, endpoint_map)
                elif action_type == "SHELL_COMMAND":
                    result = self._execute_shell_command(action)
                elif action_type == "MAIL_ACTION":
                    result = self._execute_mail_action(action)
                else:
                    result = {"error": f"Unknown action type: {action_type}"}

                status = "passed" if not result.get("error") and result.get("status") not in ["error", "failed"] else "failed"
                if status == "failed":
                    correction_count += 1

                step_data = {
                    "step": step_i, "action": action_type,
                    "details": action, "response": result,
                    "status": status
                }
                steps_log.append(step_data)
                
                # Live Recording
                if mission:
                    try:
                        AgentMissionStep.objects.create(
                            mission=mission,
                            step_number=step_i,
                            action_type=action_type,
                            thought=reason,
                            details=action,
                            response_body=str(result.get("body", result.get("stdout", result.get("error", "")))),
                            response_status=result.get("status") if isinstance(result.get("status"), int) else (result.get("exit_code") if isinstance(result.get("exit_code"), int) else None),
                            status=status,
                            screenshot_url=result.get("screenshot_url") # If available
                        )
                    except Exception as err:
                        logger.error(f"Failed to save mission step: {err}")

                # Feedback loop
                self._record_observation(f"Result for {action_type}: {json.dumps(result)}")

        except Exception as e:
            logger.error(f"[Agent] Mission Crashed: {e}")
            if mission:
                mission.status = "error"
                mission.save()
        finally:
            if self.sandbox:
                logger.info(f"[Agent] Closing sandbox: {self.sandbox.sandbox_id}")
                self.sandbox.kill()

        return steps_log

    def _execute_whitebox_setup(self, project, mission):
        """
        Clones the user's repository, injects their environment variables, and starts the server in the background.
        It then records the E2B public exposed URL for the user to 'Manual Test' the Live Sandbox.
        """
        if not self.sandbox:
            return
            
        repo_url = project.repo_url
        repo_branch = project.repo_branch or "main"
        
        logger.info(f"[Agent] (WhiteBox) Cloning {repo_url} (branch: {repo_branch})...")
        
        # 1. Clone Repo
        clone_cmd = f"git clone --branch {repo_branch} {repo_url} /app/source"
        self.sandbox.commands.run(clone_cmd)
        
        # 2. Build Environment variables mapping
        env_mapping = dict(project.environment_variables) if project.environment_variables else {}
        # Also let the frontend know we're in test mode if needed
        env_mapping["NODE_ENV"] = "development"
        
        # 3. Determine Start Command and Port based on repo_type
        # Install dependencies SYNCHRONOUSLY to avoid race conditions with agent tools
        if project.repo_type == "backend":
            target_port = 8000
            logger.info("[Agent] (WhiteBox) Installing backend dependencies (pip)...")
            # We use --upgrade to ensure we have the latest compatible versions if requested
            self.sandbox.commands.run("pip install --upgrade -r requirements.txt", cwd="/app/source", envs=env_mapping)
            start_cmd = "python manage.py runserver 0.0.0.0:8000"
        else:
            target_port = 3000
            logger.info("[Agent] (WhiteBox) Installing frontend dependencies (npm)...")
            self.sandbox.commands.run("npm install --legacy-peer-deps", cwd="/app/source", envs=env_mapping)
            start_cmd = "npm run dev"
            
        logger.info(f"[Agent] (WhiteBox) Starting server via: {start_cmd}")
        
        # 4. Start Server in Background
        self.sandbox.commands.run(
            start_cmd, 
            cwd="/app/source",
            envs=env_mapping,
            background=True
        )
        
        # 5. Wait for the server to be ready and assign exposed URL
        logger.info(f"[Agent] (WhiteBox) Waiting for server on port {target_port}...")
        time.sleep(10) # Give the frontend dev server a moment to bind
        
        try:
            # E2B creates a public URL for exposed ports
            exposed_url = self.sandbox.get_host(target_port)
            self._app_url = f"https://{exposed_url}"
            
            # Make sure the Agent knows the new local base URL
            self.extracted_vars["LOCAL_APP_URL"] = "http://localhost:" + str(target_port)
            
            # Add an initial thought step so the user knows what happened
            AgentMissionStep.objects.create(
                mission=mission,
                step_number=0,
                action_type="SHELL_COMMAND",
                thought="I have successfully cloned the repository, injected the environment variables, and started the app in the sandbox.",
                details={"command": clone_cmd + " && " + start_cmd},
                response_body=f"App is running locally at {self.extracted_vars['LOCAL_APP_URL']} and exposed publicly at {self._app_url}",
                response_status=0,
                status="passed"
            )
            logger.info(f"[Agent] (WhiteBox) Server running successfully at {self._app_url}")
        except Exception as e:
            self._app_url = None
            logger.error(f"[Agent] (WhiteBox) Failed to get host URL: {e}")

    def _execute_api_call(self, action, endpoint_map):
        """Runs an API request script inside the sandbox for state persistence."""
        endpoint_id = action.get("endpoint_id")
        target = endpoint_map.get(str(endpoint_id))
        
        if not target:
            return {"error": f"Endpoint ID '{endpoint_id}' not found."}

        resolved_url = self._resolve_url(target['url'], action.get("payload", {}).get("params", {}))
        req_data = self._prepare_request(action.get("payload", {}))
        
        # We generate a small python script to run in the sandbox
        # This ensures cookies/headers stay in the sandbox if we used a browser before
        script = f"""
import requests
import json
import time

try:
    resp = requests.request(
        method='{target['method']}',
        url='{resolved_url}',
        headers={json.dumps(req_data.get("headers", {}))},
        json={json.dumps(req_data.get("body"))} if '{target['method']}' in ['POST', 'PUT', 'PATCH'] else None,
        params={json.dumps(req_data.get("params"))},
        timeout=30
    )
    print(json.dumps({{
        "status": resp.status_code,
        "reason": resp.reason,
        "body": resp.text[:1000],
    }}))
except Exception as e:
    print(json.dumps({{"error": str(e)}}))
"""
        return self._run_in_sandbox_or_host(script)

    def _execute_shell_command(self, action):
        """Executes arbitrary CLI commands (New for E2B)."""
        cmd = action.get("command")
        if not self.sandbox:
            return {"error": "Sandbox not available for shell commands."}
        
        logger.info(f"[Agent] Executing Shell: {cmd}")
        try:
            res = self.sandbox.commands.run(cmd, timeout=30)
            return {
                "stdout": res.stdout,
                "stderr": res.stderr,
                "exit_code": res.exit_code
            }
        except Exception as e:
            return {"error": str(e)}

    def _run_in_sandbox_or_host(self, script):
        """Helper to run a python snippet in the sandbox or fallback to local."""
        if self.sandbox:
            if not self._sandbox_file_write("/home/user/agent_temp.py", script):
                logger.error("[Agent] Failed to write temp script to sandbox")
                return None
            # Force environment variables for the one-off script
            cmd = "export PLAYWRIGHT_BROWSERS_PATH=/ms-playwright && export DISPLAY=:1 && python3 /home/user/agent_temp.py"
            res = self.sandbox.commands.run(cmd)
            try:
                return json.loads(res.stdout)
            except:
                return {"stdout": res.stdout, "stderr": res.stderr, "error": "Invalid JSON output from script"}
        else:
            return {"error": "Native execution failed. Sandbox required."}

    def _start_gui_stack(self):
        """Start Xvfb + fluxbox + x11vnc + noVNC as persistent background processes.

        Strategy: upload a SINGLE setup script via ``sandbox.files.write`` (HTTP,
        no deadline issues) and execute it once with ``background=True``.  Then
        poll readiness by reading a marker file via ``sandbox.files.read`` (also
        HTTP) instead of calling ``commands.run`` for each ``pgrep`` / ``ss``
        check — every ``commands.run`` call goes through E2B's gRPC channel which
        has a hard ~5 s backend deadline that we cannot control.
        """
        logger.info("[Agent] Starting GUI stack (Xvfb + VNC)...")
        if not self.sandbox:
            logger.error("[Agent] No sandbox available for GUI stack.")
            self._vnc_url = None
            return

        # --- 1. Upload the setup script via HTTP (reliable, no deadline) ----------
        gui_setup_script = r"""#!/bin/bash
set -e

# Kill any stale daemons
pkill -x Xvfb 2>/dev/null || true
pkill -x x11vnc 2>/dev/null || true
pkill -x fluxbox 2>/dev/null || true
pkill -x websockify 2>/dev/null || true
sleep 0.5

# 1. Xvfb — virtual framebuffer
echo 'Starting Xvfb...' > /tmp/gui_setup.log
Xvfb :1 -screen 0 1280x1024x24 > /tmp/xvfb.log 2>&1 &
for i in $(seq 1 20); do
  if pgrep -x Xvfb >/dev/null 2>&1; then
    echo 'Xvfb UP' >> /tmp/gui_setup.log
    break
  fi
  sleep 0.5
done

# 2. fluxbox — window manager
DISPLAY=:1 fluxbox > /tmp/fluxbox.log 2>&1 &
sleep 1

# 3. x11vnc — VNC server
DISPLAY=:1 x11vnc -display :1 -nopw -forever -shared -rfbport 5900 > /tmp/x11vnc.log 2>&1 &
for i in $(seq 1 20); do
  if pgrep -x x11vnc >/dev/null 2>&1; then
    echo 'x11vnc UP' >> /tmp/gui_setup.log
    break
  fi
  sleep 0.5
done

# 4. noVNC via websockify — probe known paths
NOVNC_PATH=''
for p in /usr/share/novnc /usr/share/novnc/web /usr/share/websockify; do
  if [ -f "$p/vnc.html" ] || [ -f "$p/vnc_lite.html" ]; then
    NOVNC_PATH="$p"
    break
  fi
done

if [ -n "$NOVNC_PATH" ]; then
  echo "noVNC found at $NOVNC_PATH" >> /tmp/gui_setup.log
  DISPLAY=:1 websockify --web "$NOVNC_PATH" 80 localhost:5900 > /tmp/websockify.log 2>&1 &
else
  echo 'noVNC web files not found, starting websockify without web UI' >> /tmp/gui_setup.log
  websockify 80 localhost:5900 > /tmp/websockify.log 2>&1 &
fi

# 5. Wait for port 80 to be listening
for i in $(seq 1 30); do
  if ss -tlnp 2>/dev/null | grep -q ':80 '; then
    echo 'port80 OPEN' >> /tmp/gui_setup.log
    break
  fi
  sleep 1
done

# 6. Write readiness marker — we read this via sandbox.files.read (HTTP)
echo "DONE" > /tmp/gui_ready
echo "GUI stack setup complete" >> /tmp/gui_setup.log
cat /tmp/gui_setup.log >> /tmp/gui_setup_full.log
"""
        try:
            self._sandbox_file_write("/home/user/setup_gui.sh", gui_setup_script)
        except Exception as e:
            logger.error(f"[Agent] Failed to upload GUI setup script: {e}")
            self._vnc_url = None
            return

        # --- 2. Execute the script as ONE background command ---------------------
        try:
            self.sandbox.commands.run(
                "chmod +x /home/user/setup_gui.sh && bash /home/user/setup_gui.sh",
                background=True,
            )
        except Exception as e:
            logger.error(f"[Agent] Failed to launch GUI setup script: {e}")
            self._vnc_url = None
            return

        # --- 3. Poll readiness via HTTP (sandbox.files.read) — no gRPC deadline ---
        gui_ready = False
        for attempt in range(30):  # up to 30 s
            try:
                marker = self.sandbox.files.read("/tmp/gui_ready")
                content = marker if isinstance(marker, str) else getattr(marker, "content", "")
                if "DONE" in str(content):
                    gui_ready = True
                    logger.info(f"[Agent] GUI stack ready (poll #{attempt + 1})")
                    break
            except Exception:
                pass  # file doesn't exist yet
            time.sleep(1)

        if not gui_ready:
            # Read the log to understand what happened
            try:
                log_content = self.sandbox.files.read("/tmp/gui_setup.log")
                log_str = log_content if isinstance(log_content, str) else getattr(log_content, "content", "")
                logger.error(f"[Agent] GUI stack failed to become ready. Setup log:\n{log_str}")
            except Exception:
                logger.error("[Agent] GUI stack failed to become ready (no setup log available)")
            self._vnc_url = None
            return

        # --- 4. Create E2B tunnel to port 80 -----------------------------------
        try:
            vnc_host = self.sandbox.get_host(80)
            self._vnc_url = f"https://{vnc_host}"
            logger.info(f"[Agent] VNC Live View ready at: {self._vnc_url}")
        except Exception as e:
            self._vnc_url = None
            logger.warning(f"[Agent] Could not get VNC host URL: {e}")

        # --- 5. Debug: dump tail of setup log -----------------------------------
        try:
            full_log = self.sandbox.files.read("/tmp/gui_setup_full.log")
            log_str = full_log if isinstance(full_log, str) else getattr(full_log, "content", "")
            if log_str:
                logger.info(f"[Agent] GUI setup log:\n{log_str}")
        except Exception:
            pass

    def _init_browser_manager(self):
        """Starts a persistent Playwright process in the sandbox.
        
        When VNC is active, the browser ALWAYS launches non-headless so it
        renders inside the Xvfb framebuffer — visible in the live VNC stream.
        The user can watch the agent navigate and take over if needed.
        """
        if not self.sandbox:
            return

        cfg = self.browser_config or {}
        
        # KEY FIX: If VNC/GUI stack is running, force headless=False so the
        # browser renders in the framebuffer (visible via noVNC).
        # If there's NO display (no Xvfb), we MUST run headless=True or the
        # browser launch fails and we never get READY.
        has_vnc = bool(getattr(self, '_vnc_url', None))
        if has_vnc:
            is_headless = False
            logger.info("[Agent] VNC detected — running browser visible in live stream (headless=False).")
        else:
            is_headless = True
            logger.info("[Agent] No VNC display available — running browser headless.")        # Start the Browser Manager script
        # CRITICAL: Only set DISPLAY when running non-headless (VNC is up).
        # Load browser manager template from file to avoid f-string escaping issues
        # that caused SyntaxError: unterminated string literal in the inner script.
        _tmplt = os.path.join(os.path.dirname(__file__), 'browser_manager_template.py')
        with open(_tmplt, 'r') as _f:
            _script = _f.read()
        headless_str = str(is_headless)  # Must be True/False (Python bool), not true/false
        display_line = 'os.environ["DISPLAY"] = ":1"' if not is_headless else '# Headless mode — no DISPLAY needed'
        script = _script.replace('__HEADLESS__', headless_str).replace('__DISPLAY_LINE__', display_line)
        try:
            if not self._sandbox_file_write("/home/user/browser_manager.py", script):
                logger.error("[Agent] Failed to write browser manager script — sandbox may be unhealthy")
                return
            # Only set DISPLAY when running non-headless (VNC/Xvfb is up)
            if is_headless:
                bg_cmd = "export PLAYWRIGHT_BROWSERS_PATH=/ms-playwright && python3 -u /home/user/browser_manager.py"
            else:
                bg_cmd = "export PLAYWRIGHT_BROWSERS_PATH=/ms-playwright && export DISPLAY=:1 && python3 -u /home/user/browser_manager.py"
            self.browser_process = self.sandbox.commands.run(bg_cmd, background=True)
            # Wait for READY signal with timeout (20s — Playwright launch can be slow)
            ready = False
            start = time.time()
            for out, err, pty in self.browser_process:
                if out and "READY" in out:
                    logger.info("[Agent] Persistent browser manager is READY — visible in VNC live stream." if not is_headless else "[Agent] Persistent browser manager is READY (headless mode).")
                    ready = True
                    break
                if err and err.strip():
                    logger.warning(f"[Agent] Browser manager stderr: {err.strip()}")
                if time.time() - start > 20:
                    logger.error("[Agent] Browser manager timed out waiting for READY signal (20s)")
                    break
            if not ready:
                # Try to read the error log for debugging
                try:
                    err_content = self.sandbox.files.read("/tmp/browser_manager.err")
                    err_str = err_content if isinstance(err_content, str) else getattr(err_content, "content", "")
                    if err_str:
                        logger.error(f"[Agent] Browser manager never sent READY. Error log: {str(err_str).strip()[:500]}")
                    else:
                        logger.error("[Agent] Browser manager never sent READY — no error log found")
                except Exception:
                    logger.error("[Agent] Browser manager never sent READY — falling back to ephemeral browser")
                self.browser_process = None
        except Exception as e:
            logger.error(f"[Agent] Failed to start browser manager: {e}")
            self.browser_process = None

    def _execute_browser_action(self, action):
        """Sends an action to the persistent browser manager."""
        # Verify and install playwright binary dependencies exactly before first use
        if not getattr(self, '_playwright_verified', False):
            self._ensure_playwright_browsers()
            self._playwright_verified = True
            
        # Ensure browser manager is active — restart if it crashed
        # But don't retry endlessly — if it failed once, go straight to legacy
        if not getattr(self, 'browser_process', None):
            if not getattr(self, '_browser_manager_failed', False):
                self._init_browser_manager()
                if not getattr(self, 'browser_process', None):
                    self._browser_manager_failed = True
                    logger.warning("[Agent] Browser manager permanently failed — using legacy browser for all browser actions")
        else:
            # Check if the process is still alive by trying to read
            try:
                # Quick health check — if pid is gone, restart
                test_send = self.sandbox.commands.send_stdin(self.browser_process.pid, '\n')
            except Exception:
                logger.warning("[Agent] Browser process died — restarting...")
                self.browser_process = None
                self._init_browser_manager()

        if not getattr(self, 'browser_process', None):
            # Fallback to ephemeral method if persistent manager failed
            return self._execute_browser_action_legacy(action)

        try:
            curr_action = {
                "action": action.get("action"),
                "url": action.get("url"),
                "selector": action.get("selector"),
                "value": action.get("value")
            }
            # Send action as JSON line via sandbox.commands.send_stdin
            self.sandbox.commands.send_stdin(self.browser_process.pid, json.dumps(curr_action) + "\n")
            
            # Read response (one line of JSON) from the generator
            for out, err, pty in self.browser_process:
                if out and out.strip():
                    try:
                        res = json.loads(out)
                        # Post-process: Vision
                        if res.get("screenshot_b64"):
                            res["visual_observation"] = self._analyze_vision(res["screenshot_b64"])
                            del res["screenshot_b64"]
                        return res
                    except json.JSONDecodeError:
                        continue # Skip non-json lines
        except Exception as e:
            logger.error(f"[Agent] Error in persistent browser action: {e}")
            return {"error": str(e)}

    def _execute_browser_action_legacy(self, action):
        """Original ephemeral browser execution (Fallback).
        When VNC is active, forces headless=False so the browser renders in the framebuffer.
        """
        logger.info(f"[Agent] Using Legacy Ephemeral Browser for: {action.get('action')}")
        cfg = self.browser_config or {}
        b_type = (cfg.get("browser") or "chromium").lower()
        
        # KEY FIX: Respect VNC — if GUI stack is running, show the browser in VNC
        has_vnc = bool(getattr(self, '_vnc_url', None))
        is_headless = cfg.get("headless", True) and not has_vnc
        
        if b_type == "firefox":
            browser_type_override = f"browser = p.firefox.launch(headless={is_headless})"
        elif b_type in ["webkit", "safari"]:
            browser_type_override = f"browser = p.webkit.launch(headless={is_headless})"
        else:
            browser_type_override = f"browser = p.chromium.launch(headless={is_headless})"
            
        context_opts = []
        if cfg.get("device"):
            context_opts.append(f"**p.devices['{cfg.get('device')}']")
        if cfg.get("geolocation"):
            geo = cfg.get('geolocation')
            context_opts.append(f"geolocation={{'latitude': {geo.get('latitude', 0)}, 'longitude': {geo.get('longitude', 0)}}}, permissions=['geolocation']")
            
        context_str = ", ".join(context_opts)
        context_launch = f"context = browser.new_context({context_str})" if context_opts else "context = browser.new_context()"
        
        throttle_block = ""
        if cfg.get("network") and b_type == "chromium":
            speed_map = {
                "Fast_3G": "{'offline': False, 'downloadThroughput': 1.6 * 1024 * 1024 / 8, 'uploadThroughput': 750 * 1024 / 8, 'latency': 150}",
                "Slow_3G": "{'offline': False, 'downloadThroughput': 500 * 1024 / 8, 'uploadThroughput': 500 * 1024 / 8, 'latency': 400}",
                "Offline": "{'offline': True, 'downloadThroughput': 0, 'uploadThroughput': 0, 'latency': 0}",
            }
            if cfg.get("network") in speed_map:
                throttle_block = f"""
            client = context.new_cdp_session(page)
            client.send('Network.emulateNetworkConditions', {speed_map[cfg.get('network')]})
                """

        script = f"""
import os
os.environ['PLAYWRIGHT_BROWSERS_PATH'] = '/ms-playwright'
from playwright.sync_api import sync_playwright
import json
import base64

def run():
    with sync_playwright() as p:
        {browser_type_override}
        {context_launch}
        page = context.new_page()
        {throttle_block}
        
        try:
            # Action
            action_type = {repr(action.get('action'))}
            if action_type == 'navigate':
                page.goto({repr(action.get('url'))})
            elif action_type == 'click':
                page.click({repr(action.get('selector'))})
            elif action_type == 'type':
                page.fill({repr(action.get('selector'))}, {repr(action.get('value'))})
            
            page.wait_for_timeout(2000)
            
            # Capture
            screenshot = base64.b64encode(page.screenshot()).decode('utf-8')
            
            print(json.dumps({{
                "status": "success",
                "title": page.title(),
                "screenshot_b64": screenshot,
                "html_preview": page.content()[:500]
            }}))
        except Exception as e:
            print(json.dumps({{"error": str(e)}}))
        finally:
            browser.close()

run()
"""
        res = self._run_in_sandbox_or_host(script)
        if res.get("screenshot_b64"):
            res["visual_observation"] = self._analyze_vision(res["screenshot_b64"])
            del res["screenshot_b64"]
        return res

    def _analyze_vision(self, b64_image):
        """Uses the Vision LLM to 'see' the screenshot."""
        prompt = "Describe what you see on this screen. Identify any visible error messages, broken layouts, or if the page looks correct according to the action performed. Be concise."
        
        try:
            response = litellm.completion(
                model=self.model,
                messages=[{
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64_image}"}}
                    ]
                }],
                max_tokens=300
            )
            return response.choices[0].message.content
        except Exception as e:
            logger.error(f"Vision Analysis Failed: {e}")
            return "Vision system unavailable, relying on HTML observation."
    def _ensure_playwright_browsers(self):
        """Verifies and installs playwright binaries if missing or incompatible."""
        if not self.sandbox:
            return
            
        logger.info("[Agent] Verifying Playwright binaries in sandbox...")
        
        # Force global browser path in shell command directly to avoid SDK argument mismatch
        check_cmd = "export PLAYWRIGHT_BROWSERS_PATH=/ms-playwright && python3 -c 'import os; os.environ[\"PLAYWRIGHT_BROWSERS_PATH\"]=\"/ms-playwright\"; from playwright.sync_api import sync_playwright; p=sync_playwright().start(); p.chromium.launch(headless=True).close(); p.stop()'"
        
        needs_install = False
        try:
            # Increase connection timeout for verification
            res = self.sandbox.commands.run(check_cmd, timeout=60)
            if res.exit_code != 0:
                logger.warning(f"[Agent] Playwright verification failed (exit {res.exit_code}). Output: {res.stderr}")
                needs_install = True
        except Exception as e:
            logger.warning(f"[Agent] Playwright verification check crashed: {e}. Attempting repair...")
            needs_install = True
        
        if needs_install:
            logger.info("[Agent] Running deep repair: playwright install --with-deps chromium")
            try:
                repair_cmd = "export PLAYWRIGHT_BROWSERS_PATH=/ms-playwright && playwright install --with-deps chromium"
                # Increase connection timeout for installation (10 mins)
                install_res = self.sandbox.commands.run(repair_cmd, timeout=600)
                if install_res.exit_code == 0:
                    logger.info("[Agent] Deep repair successful.")
                else:
                    logger.error(f"[Agent] Deep repair failed with exit code {install_res.exit_code}.")
            except Exception as e:
                logger.error(f"[Agent] Critical error during Playwright repair: {e}")
        else:
            logger.info("[Agent] Playwright binaries verified.")


    def _execute_stress_test(self, action, endpoint_map):
        """Executes a Locust load test inside the E2B Sandbox."""
        if not self.sandbox:
            return {"error": "Sandbox not available for stress tests."}
            
        target_ids = action.get("endpoints_to_hit", [])
        users = action.get("users", 10)
        
        # We can dynamically pass the data to the pre-installed locustfile
        logger.info(f"[Agent] E2B Stress Test requested for {len(target_ids)} endpoints.")
        
        # For simplicity in this refactor, we just run a basic locust command
        # A more advanced version would use the locustfile we copied into the template
        cmd = f"locust -f /home/user/locustfile.py --headless -u {users} -r 2 -t 30s"
        return self._execute_shell_command({"command": cmd})

    def _execute_mail_action(self, action):
        """Manages disposable email addresses using Mail.tm."""
        mail_type = action.get("action") # 'create', 'get_messages', 'get_otp'
        
        try:
            # 1. Create a new address
            if mail_type == "create":
                # Get domain first
                domain_resp = requests.get("https://api.mail.tm/domains").json()
                domain = domain_resp['member'][0]['domain']
                username = f"agent_{int(time.time())}"
                password = "password123"
                email = f"{username}@{domain}"
                
                resp = requests.post("https://api.mail.tm/accounts", json={
                    "address": email,
                    "password": password
                })
                if resp.status_code != 201:
                    return {"error": f"Failed to create email: {resp.text}"}
                
                # Get Token
                token_resp = requests.post("https://api.mail.tm/token", json={
                    "address": email,
                    "password": password
                })
                self.mail_session = token_resp.json()['token']
                self.current_email = email
                self.extracted_vars["AGENT_EMAIL"] = email
                
                return {"status": "success", "email": email, "reason": "New real disposable email created"}

            # 2. Get Messages
            if not self.mail_session:
                return {"error": "No active mail session. Call 'create' first."}
                
            headers = {"Authorization": f"Bearer {self.mail_session}"}
            msgs_resp = requests.get("https://api.mail.tm/messages", headers=headers).json()
            
            if not msgs_resp.get('member'):
                return {"status": "waiting", "message": "No emails found yet."}

            latest_msg = msgs_resp['member'][0]
            
            # 3. Get Full Content if needed
            msg_detail = requests.get(f"https://api.mail.tm/messages/{latest_msg['id']}", headers=headers).json()
            body = msg_detail.get('text') or msg_detail.get('html', "")
            
            # Simple OTP extraction helper
            otp_match = re.search(r'\b\d{4,6}\b', body)
            otp = otp_match.group(0) if otp_match else None

            return {
                "status": "success",
                "subject": latest_msg.get('subject'),
                "body": body[:500] + "...",
                "extracted_otp": otp,
                "created_at": latest_msg.get('createdAt')
            }

        except Exception as e:
            return {"error": str(e)}

    def _fetch_global_credentials(self):
        """
        Retrieves managed accounts for external auth from the DB.

        Credentials are scoped: shared (project-less) creds apply everywhere,
        plus any credentials that were saved against this mission's project.
        This keeps test logins out of unrelated projects.
        """
        try:
            from django.db.models import Q
            from users.models import TestCredential
            creds = TestCredential.objects.filter(is_active=True).filter(
                Q(project__isnull=True) | Q(project_id=self.collection.project_id)
            )
            return [
                {
                    "provider": c.provider,
                    "email": c.email,
                    "password": c.decrypted_password,
                    "description": c.description,
                    "metadata": c.metadata
                }
                for c in creds
            ]
        except Exception as e:
            logger.error(f"[Agent] Failed to fetch global creds: {e}")
            return []

    def _build_scenario_directives(self):
        """Returns scenario-specific testing directives based on the active scenarios."""
        directives = []
        scenarios = self.scenarios if isinstance(self.scenarios, list) else [self.scenarios]
        
        scenario_guide = {
            'HAPPY_PATH': (
                "HAPPY PATH: Test the primary success flow end-to-end. "
                "Start with authentication, then hit the core CRUD operations in logical order. "
                "Verify 2xx responses, correct response bodies, and proper resource creation."
            ),
            'SECURITY': (
                "SECURITY: Perform adversarial testing on every endpoint:\n"
                "  - Broken Authentication: Call endpoints with no token, expired token, malformed token\n"
                "  - IDOR: Change resource IDs in requests to access other users' data\n"
                "  - Injection: Send SQL injection payloads in string fields (', \"; DROP TABLE--), XSS payloads (<script>alert(1)</script>)\n"
                "  - Privilege Escalation: Access admin endpoints as a regular user\n"
                "  - Mass Assignment: Add unexpected fields (role, is_admin, balance) to request bodies\n"
                "  Expected: 401/403 for unauthorized access. Any 2xx for unauthorized is a VULNERABILITY."
            ),
            'EDGE_CASE': (
                "EDGE CASES: Test boundary conditions and unusual inputs:\n"
                "  - Empty strings, null values, extremely long strings (10000+ chars)\n"
                "  - Negative numbers, zero, floats where ints expected\n"
                "  - Unicode/emoji in text fields\n"
                "  - Special characters: { }, [ ], < >, \\n\n\r\n"
                "  - Very large numbers (Number.MAX_SAFE_INTEGER+)\n"
                "  - Missing optional fields vs required fields"
            ),
            'VALIDATION_ERROR': (
                "VALIDATION ERRORS: Test input validation thoroughly:\n"
                "  - Send requests with missing required fields one at a time\n"
                "  - Send wrong types (string where number expected, array where object expected)\n"
                "  - Verify error responses include helpful messages and correct HTTP status codes\n"
                "  - Check that invalid data is NOT persisted (GET the resource after failed POST)"
            ),
            'PERFORMANCE': (
                "PERFORMANCE: Use STRESS_TEST tool to load-test critical endpoints:\n"
                "  - Test with 10-50 concurrent users on key endpoints\n"
                "  - Focus on endpoints that handle authentication, data creation, and search\n"
                "  - Look for response times > 2s or error rates > 5%"
            ),
            'REGRESSION': (
                "REGRESSION: Re-run previously failed test scenarios and verify fixes.\n"
                "  - Look at the PAST FAILURES section below for specific failures to re-test\n"
                "  - Re-execute the EXACT same actions that failed before\n"
                "  - Verify each failure is now fixed (returns expected status/response)\n"
                "  - If a past failure was a 500 error, confirm it now returns 2xx\n"
                "  - If a past failure was a timeout, confirm the operation completes\n"
                "  - Report each as FIXED or STILL BROKEN\n"
                "  - Do NOT skip any past failure"
            ),
            'SMOKE': (
                "SMOKE: Quick validation that core functionality works:\n"
                "  - Test only the most critical 3-5 endpoints\n"
                "  - Verify authentication works\n"
                "  - Verify the main resource CRUD cycle\n"
                "  - Stop after confirming basic functionality."
            ),
            'E2E': (
                "E2E (End-to-End): Test complete user journeys through the full stack:\n"
                "  - Use BROWSER_ACTION to navigate the UI\n"
                "  - Combine API calls with browser interactions\n"
                "  - Test the full flow: signup → login → create → view → edit → delete\n"
                "  - Verify data persists across page refreshes"
            ),
        }
        
        for s in scenarios:
            key = s.upper().replace(' ', '_').replace('-', '_')
            if key in scenario_guide:
                directives.append(f">> {scenario_guide[key]}")
        
        return '\n\n'.join(directives) if directives else ">> Follow the general instructions below."

    def _build_regression_context(self):
        """
        Injects specific past failure context into the agent prompt.
        When previous_failures is populated, the agent knows EXACTLY what to re-test.
        """
        if not self.previous_failures:
            return ""

        failures_text = "\n"
        for i, f in enumerate(self.previous_failures, 1):
            failures_text += (
                f"  FAILURE #{i}:\n"
                f"    Action: {f.get('action_type', 'UNKNOWN')}\n"
                f"    What was attempted: {f.get('thought', 'No details')}\n"
                f"    HTTP Status: {f.get('response_status', 'N/A')}\n"
                f"    Error/Response: {(f.get('response_body') or '')[:300]}\n\n"
            )

        return (
            "PAST FAILURES FROM PREVIOUS SESSION (Regression Baseline):\n"
            f"The previous test session had {len(self.previous_failures)} failure(s). "
            "Your PRIMARY job is to RE-TEST each of these failures and verify if they are now fixed.\n\n"
            f"{failures_text}\n"
            "REGRESSION RULES:\n"
            "1. Re-run the EXACT same action type and endpoint that failed before.\n"
            "2. If the failure was a specific HTTP status (e.g. 500), verify the same request now returns the correct status.\n"
            "3. If the failure was a crash/timeout, retry the same operation.\n"
            "4. Report each failure as FIXED (now passes) or STILL BROKEN (still fails).\n"
            "5. Do NOT skip any past failure — test ALL of them.\n"
            "6. After testing all past failures, you may explore new areas if time permits.\n"
        )

    SECRET_KEY_PATTERNS = ('key', 'secret', 'token', 'password', 'passwd', 'credential', 'auth', 'api', 'private')

    @classmethod
    def redact_env_vars(cls, env_vars):
        """Mask secret-looking env var values before they reach the AI.
        Returns names + masked values so the agent knows a var EXISTS (and can
        reference it by name in sandbox shell commands) without the raw secret
        ever leaving the platform."""
        redacted = {}
        for k, v in (env_vars or {}).items():
            k_lower = k.lower()
            if any(p in k_lower for p in cls.SECRET_KEY_PATTERNS):
                masked = (str(v)[:3] + "****") if len(str(v)) > 6 else "****"
                redacted[k] = f"[REDACTED - {masked}] (use sandbox env or credentials; do not ask the user)"
            else:
                redacted[k] = v
        return redacted

    def _build_system_prompt(self, endpoints):
        # Tools are enabled based on runner_types
        has_http = "http" in self.runner_types
        has_browser = "browser" in self.runner_types
        has_load = "load" in self.runner_types
        
        creds = self._fetch_global_credentials()
        
        # Extract structured context from project
        project = self.collection.project
        auth_type = getattr(project, 'auth_type', 'none') or 'none'
        tech_stack = getattr(project, 'tech_stack', {}) or {}
        critical_flows = getattr(project, 'critical_flows', []) or []
        domain_rules = getattr(project, 'domain_rules', '') or ''
        
        # Build structured context sections
        auth_section = f"""
AUTHENTICATION TYPE: {auth_type}
"""
        
        tech_section = ""
        if tech_stack:
            tech_parts = []
            for category in ['frontend', 'backend', 'database']:
                items = tech_stack.get(category, [])
                if items:
                    tech_parts.append(f"- {category.title()}: {', '.join(items)}")
            if tech_parts:
                tech_section = f"""\nTECH STACK:
{chr(10).join(tech_parts)}
"""
        
        flows_section = ""
        if critical_flows:
            flow_lines = []
            for f in critical_flows:
                name = f.get('name', 'Unnamed')
                priority = f.get('priority', 'P1')
                desc = f.get('description', '')
                flow_lines.append(f"- [{priority}] {name}{': ' + desc if desc else ''}")
            flows_section = f"""\nCRITICAL USER FLOWS (prioritized — focus testing effort here):
{chr(10).join(flow_lines)}
"""
        
        domain_section = ""
        if domain_rules:
            domain_section = f"""\nDOMAIN RULES & CONSTRAINTS:
{domain_rules}
"""
        
        preferred_types = getattr(project, 'preferred_test_types', []) or []
        preferred_section = ""
        if preferred_types:
            preferred_section = f"""\nPROJECT PREFERRED TEST TYPES (selected during project setup):
{', '.join(preferred_types)}
These are the testing priorities the user chose. Align your MISSION SCENARIOS with these preferences.
"""

        return f"""
You are an Universal Autonomous QA Agent. You have the "Vibe" of a senior human tester.
Your goal is to verify the User Story: "{self.user_story}"

MISSION PROFILE:
- CATEGORIES: {", ".join(self.categories)}
- TARGET LAYER: {self.layer}
- MISSION SCENARIOS: {self.scenarios} (Focus your thinking on these types of tests)
{auth_section}{preferred_section}{tech_section}{flows_section}{domain_section}
TARGET ENVIRONMENT:
- BASE URL: {self.collection.base_url or "None provided"}
- PROJECT VARS: {json.dumps(self.redact_env_vars(self.env_vars))}

GLOBAL TEST CREDENTIALS (Use these if you encounter external/3rd-party auth screens): 
{json.dumps(creds, indent=2)}

AVAILABLE API ENDPOINTS:
{json.dumps(endpoints, indent=2) if endpoints else "None provided - Use BROWSER_ACTION or SHELL_COMMAND to explore the target URL directly."}

YOUR TOOLSET:
{"1. CALL_API: Use this for functional backend testing. Runs inside the sandbox." if has_http else ""}
{"2. BROWSER_ACTION: Use this to test the UI/UX via Playwright inside the sandbox." if has_browser else ""}
{"3. STRESS_TEST: Use this if the user wants to test performance." if has_load else ""}
4. SHELL_COMMAND: Use this to run any CLI commands, check files, or use Linux tools.
   - For Security Missions, use: `sqlmap`, `nmap`, `zap-cli`, `gitleaks` (for secrets in code), or `owasp-dependency-check`.
5. MAIL_ACTION: Use this to handle OTPs and real email verification.
   - Use 'create' to get a new address.
   - Use 'get_messages' to check the inbox and find OTP codes.

SCENARIO-SPECIFIC DIRECTIVES:
{self._build_scenario_directives()}

{self._build_regression_context()}
INSTRUCTIONS:
1. EXPLORATION DEPTH & PIVOT: Your core goal is to verify ALL MISSION SCENARIOS: {self.scenarios}.
   - BE THOROUGH: Do not just try one thing and move on. Attempt at least 2-3 different variations per scenario on high-impact endpoints.
   - PIVOTING: Once a feature has been "stressed" with variations, pivot to the next scenario or endpoint.
   - CRITICAL: You are NOT ALLOWED to call 'FINISH' until you have actually performed multiple tangible actions for each mission scenario.
2. AUTHENTICATION STRATEGY:
   - FOR 3RD-PARTY AUTH (Google/GitHub/Social): Use the `GLOBAL TEST CREDENTIALS` provided. Click the social login button and type the corresponding email/password.
   - FOR STANDARD EMAIL SIGNUP/LOGIN: Use `MAIL_ACTION` tool. Use 'create' to get a `AGENT_EMAIL` before signup. Use 'get_messages' to retrieve OTPs.
   - For OTP/VERIFICATION FLOW: MAIL_ACTION 'create' BEFORE signup → use 'AGENT_EMAIL' in signup → MAIL_ACTION 'get_messages' to get OTP → verify.
3. COMPLIANCE CHECKLIST: Before every move, mentally check off which scenarios from {self.scenarios} you have already verified. Do not finish until you have diverse coverage for ALL of them.
4. SCHEMA OBSESSION: Before calling any API, check its 'request_body' field in the AVAILABLE API ENDPOINTS list. This is your MANDATORY template. Match its keys and casing EXACTLY. Also check 'auth_type' and 'headers' per endpoint.
5. ADAPT: If an API call fails (4xx/5xx), ANALYZE THE ERROR BODY for the correct keys. If the server says "FirstName is required", look at your casing! Use the exact keys the server's error message suggests.
{"6. UI EXPLORATION: If 'AVAILABLE API ENDPOINTS' is empty but you have a 'BASE URL', start by using BROWSER_ACTION 'navigate' to the BASE URL to discover the application." if has_browser else ""}
7. SAFE MODE GUARDRAILS: {"ENABLED" if self.is_safe_mode else "DISABLED"}
   - {"Since Safe Mode is ENABLED: You are strictly forbidden from performing destructive actions (DELETE, PUT/PATCH that updates sensitive data) on PRODUCTION URLs. Only perform READ operations or safe creations." if self.is_safe_mode else "Since Safe Mode is DISABLED: You may perform destructive actions to test exploitation, but only if necessary to verify the scenario."}
8. MISSION COMPLETE: You successfully finish when the intent of the User Story is verified.
   - This usually means 2xx success, but if the story is a "Negative Test" (e.g., "Verify that unauthenticated users get blocked"), then a 403/401 is actually your goal!
   - Explain your result clearly in the FINISH reason.

OUTPUT FORMAT (JSON ONLY):
Return a JSON object:

Type A: CALL_API
{{
  "type": "CALL_API",
  "endpoint_id": "...",
  "reason": "Explain why this step matters for the story",
  "payload": {{ "headers": {{...}}, "body": {{...}}, "params": {{...}} }}
}}

Type B: BROWSER_ACTION
{{
  "type": "BROWSER_ACTION",
  "action": "navigate/click/type/check",
  "url": "...",
  "selector": "css selector if clicking/typing",
  "value": "text to type if action is 'type'",
  "reason": "I need to see if the dashboard loads after login"
}}

Type C: STRESS_TEST
{{
  "type": "STRESS_TEST",
  "scenario_name": "...",
  "endpoints_to_hit": ["endpoint_id_1", "endpoint_id_2"],
  "users": 100,
  "spawn_rate": 10,
  "reason": "The user wants to ensure the signup flow doesn't crash under pressure"
}}

Type D: SHELL_COMMAND
{{
  "type": "SHELL_COMMAND",
  "command": "ls -la /app",
  "reason": "I need to check if the build artifacts was generated successfully"
}}

Type E: MAIL_ACTION
{{
  "type": "MAIL_ACTION",
  "action": "create",
  "reason": "I need a real email to sign up"
}}

Type F: FINISH
{{ "type": "FINISH", "reason": "Story fully verified." }}
"""

    def _get_next_action(self):
        """Calls LLM to decide the next move based on context."""
        try:
            # Refresh the context: Let the agent know what it currently 'knows'
            memory_context = {
                "role": "system", 
                "content": f"CURRENT MEMORY (Variables available): {json.dumps(self.extracted_vars)}"
            }
            
            # We don't want to bloat the history, so we just temporarily insert/update the memory
            messages = [self.history[0]] + [memory_context] + self.history[1:]

            response = litellm.completion(
                model=self.model,
                messages=messages,
                response_format={"type": "json_object"}
            )
            content = response.choices[0].message.content
            
            # Append assistant's thought to history
            self.history.append({"role": "assistant", "content": content})
            
            # Robust JSON Parsing
            try:
                action = json.loads(content)
                # If the LLM returned a list of actions, just take the first one
                if isinstance(action, list) and len(action) > 0:
                    action = action[0]
                
                if not isinstance(action, dict):
                    logger.error(f"Agent Action is not a dict: {type(action)}")
                    return {"type": "ERROR", "reason": "LLM returned invalid action format (not a dict)."}
                
                return action
            except json.JSONDecodeError as je:
                logger.error(f"Agent JSON Parse Error: {je} | Raw: {content}")
                return {"type": "ERROR", "reason": f"LLM returned non-JSON content: {content[:100]}"}
                
        except Exception as e:
            logger.error(f"Agent Brain Failure: {e}")
            # Sanitize error for user display
            err_str = str(e)
            if '404' in err_str and 'Not Found' in err_str:
                reason = 'The AI model returned an error (404). The model may not be available for your API key.'
            elif '429' in err_str:
                reason = 'Rate limited by the AI service. Please wait and try again.'
            elif '503' in err_str:
                reason = 'The AI service is temporarily unavailable. Please try again shortly.'
            elif 'timeout' in err_str.lower() or 'timed out' in err_str.lower():
                reason = 'The AI service timed out. The request may have been too complex.'
            else:
                reason = f'Agent encountered an error: {err_str[:200]}'
            return {"type": "FINISH", "reason": reason}

    def _record_observation(self, text):
        """Feeds the result of an action back into the brain."""
        self.history.append({"role": "user", "content": f"OBSERVATION: {text}"})

    def _prepare_request(self, payload):
        """Resolves dynamic variables in the request payload."""
        # The Agent is smart enough to put values directly in JSON, but we can add a layer here 
        # if we want to support {{var}} syntax injects. For now, we trust the Agent's generated JSON.
        return payload

    def _resolve_url(self, url_path, params=None):
        """Combines base URL with path and resolves path parameters like {id}."""
        import re
        
        final_path = url_path
        # Interpolate path parameters if they exist in params
        if params:
            for key, value in params.items():
                pattern = f"{{{key}}}"
                if pattern in final_path:
                    final_path = final_path.replace(pattern, str(value))
        
        if final_path.startswith("http"):
            return final_path
        
        base = self.collection.base_url or ""
        if base and not base.endswith("/"): base += "/"
        return base + final_path.lstrip("/")

    def _truncate_body(self, text, limit=500):
        if len(text) > limit:
            return text[:limit] + "... [truncated]"
        return text

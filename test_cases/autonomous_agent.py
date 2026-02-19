import logging
import time
import requests
import re
from django.conf import settings

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
    def __init__(self, collection, user_story=None, env_vars=None, scenarios=None, categories=None, layer="backend", runner_types=None):
        self.collection = collection
        self.user_story = user_story or "Explore the API and ensure core functionality works."
        self.env_vars = env_vars or {}
        
        # Mission Context (Determines the agent's focus)
        self.scenarios = scenarios or "HAPPY_PATH"
        self.categories = categories or ["functional"]
        self.layer = layer
        self.runner_types = runner_types or ["http"]
        
        # LLM Config
        self.provider = getattr(settings, 'LLM_PROVIDER', 'gemini').lower() 
        self.openai_api_key = getattr(settings, 'LLM_API_KEY', None)
        self.gemini_api_key = getattr(settings, 'GEMINI_API_KEY', None)
        self.e2b_api_key = getattr(settings, 'E2B_API_KEY', None)
        self.sandbox_template = getattr(settings, 'E2B_SANDBOX_TEMPLATE', 'qai-runner')
        
        if self.provider == "gemini":
            self.model = getattr(settings, 'GEMINI_MODEL', 'gemini-2.5-flash')
        else:
            self.model = getattr(settings, 'OPENAI_MODEL', 'gpt-4o-mini')

        # Agent Memory
        self.history = [] 
        self.extracted_vars = {} 
        self.sandbox = None # Initialized during mission
        self.mail_session = None # Mail.tm session
        self.current_email = None
        
        if self.env_vars:
            self.extracted_vars.update(self.env_vars)

    def run_mission(self, max_steps=10):
        """
        Executes the agent loop until the user story is satisfied or max_steps reached.
        Returns a list of steps performed with results.
        """
        logger.info(f"[Agent] Starting mission for {self.collection.name}: {self.user_story}")
        
        # 1. Understudy: Initialize Context
        # We now include the schema and ensure IDs are strings for the JSON prompt.
        endpoints_raw = list(self.collection.endpoints.values(
            'id', 'method', 'url', 'name', 'description', 
            'request_body', 'query_params'
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
                self.sandbox = Sandbox(template=self.sandbox_template, api_key=self.e2b_api_key)
            except Exception as e:
                logger.error(f"[Agent] Failed to spawn sandbox: {e}")

        system_prompt = self._build_system_prompt(endpoints)
        self.history.append({"role": "system", "content": system_prompt})
        
        steps_log = []
        correction_count = 0

        try:
            # 3. The Loop
            for step_i in range(1, max_steps + 1):
                logger.info(f"--- [Step {step_i}/{max_steps}] Thinking ---")
                
                action = self._get_next_action()
                reason = action.get('reason', 'No reason provided')
                action_type = action.get('type', 'UNKNOWN')
                logger.info(f"[Agent Decision] Type: {action_type} | Reason: {reason}")
                
                if action.get("type") == "FINISH":
                    logger.info(f"[Agent] Mission Complete: {action.get('reason')}")
                    steps_log.append({
                        "step": step_i, "action": "FINISH", "reason": action.get("reason"),
                        "self_correction_count": correction_count, "status": "success"
                    })
                    break
                    
                if action.get("type") == "ERROR":
                    correction_count += 1
                    self._record_observation(f"Error from Brain: {action.get('reason')}. Retrying.")
                    continue

                # DISPATCH TO SANDBOX
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

                steps_log.append({
                    "step": step_i, "action": action_type,
                    "details": action, "response": result,
                    "status": status
                })
                
                # Feedback loop
                self._record_observation(f"Result for {action_type}: {json.dumps(result)}")

        finally:
            if self.sandbox:
                logger.info(f"[Agent] Closing sandbox: {self.sandbox.sandbox_id}")
                self.sandbox.close()

        return steps_log

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
            self.sandbox.files.write("/home/user/agent_temp.py", script)
            res = self.sandbox.commands.run("python3 /home/user/agent_temp.py")
            try:
                return json.loads(res.stdout)
            except:
                return {"stdout": res.stdout, "stderr": res.stderr, "error": "Invalid JSON output from script"}
        else:
            # Fallback (Original method)
            import execjs # or similar, but let's just stick to the sandbox-first approach
            return {"error": "Native execution failed. Sandbox required."}

    def _execute_browser_action(self, action):
        """Executes a Playwright script inside the E2B Sandbox."""
        if not self.sandbox:
            return {"error": "Sandbox not available for browser actions."}

        logger.info(f"[Agent] E2B Browser Action: {action.get('action')} on {action.get('url') or action.get('selector')}")
        
        # We write a standalone script to the sandbox that uses its local Playwright
        script = f"""
from playwright.sync_api import sync_playwright
import json
import base64

def run():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        
        try:
            # Action
            action_type = '{action.get("action")}'
            if action_type == 'navigate':
                page.goto('{action.get("url")}')
            elif action_type == 'click':
                page.click('{action.get("selector")}')
            elif action_type == 'type':
                page.fill('{action.get("selector")}', '{action.get("value")}')
            
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
        
        # Post-process: Analyze with Vision if we got a screenshot
        if res.get("screenshot_b64"):
            res["visual_observation"] = self._analyze_vision(res["screenshot_b64"])
            del res["screenshot_b64"] # Clean up the memory
            
        return res

    def _analyze_vision(self, b64_image):
        """Uses the Vision LLM to 'see' the screenshot."""
        prompt = "Describe what you see on this screen. Identify any visible error messages, broken layouts, or if the page looks correct according to the action performed. Be concise."
        
        try:
            if self.provider == "gemini":
                # Use Gemini 1.5/2.5 Flash for vision
                model_name = "gemini-1.5-flash" # Optimized for vision
                url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent?key={self.gemini_api_key}"
                payload = {
                    "contents": [{
                        "parts": [
                            {"text": prompt},
                            {"inline_data": {"mime_type": "image/png", "data": b64_image}}
                        ]
                    }]
                }
                resp = requests.post(url, json=payload, timeout=30)
                resp.raise_for_status()
                return resp.json()["candidates"][0]["content"]["parts"][0]["text"]
            else:
                # OpenAI Vision
                url = "https://api.openai.com/v1/chat/completions"
                payload = {
                    "model": "gpt-4o-mini",
                    "messages": [{
                        "role": "user",
                        "content": [
                            {"type": "text", "text": prompt},
                            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64_image}"}}
                        ]
                    }],
                    "max_tokens": 300
                }
                headers = {"Authorization": f"Bearer {self.openai_api_key}", "Content-Type": "application/json"}
                resp = requests.post(url, json=payload, headers=headers, timeout=30)
                resp.raise_for_status()
                return resp.json()["choices"][0]["message"]["content"]
        except Exception as e:
            logger.error(f"Vision Analysis Failed: {e}")
            return "Vision system unavailable, relying on HTML observation."

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

    def _build_system_prompt(self, endpoints):
        # Tools are enabled based on runner_types
        has_http = "http" in self.runner_types
        has_browser = "browser" in self.runner_types
        has_load = "load" in self.runner_types

        return f"""
You are an Universal Autonomous QA Agent. You have the "Vibe" of a senior human tester.
Your goal is to verify the User Story: "{self.user_story}"

MISSION PROFILE:
- CATEGORIES: {", ".join(self.categories)}
- TARGET LAYER: {self.layer}
- MISSION SCENARIOS: {self.scenarios} (Focus your thinking on these types of tests)

PROJECT VARS (Auth tokens, Base URLs): {json.dumps(self.env_vars)}

AVAILABLE API ENDPOINTS:
{json.dumps(endpoints, indent=2)}

YOUR TOOLSET:
{"1. CALL_API: Use this for functional backend testing. Runs inside the sandbox." if has_http else ""}
{"2. BROWSER_ACTION: Use this to test the UI/UX via Playwright inside the sandbox." if has_browser else ""}
{"3. STRESS_TEST: Use this if the user wants to test performance." if has_load else ""}
4. SHELL_COMMAND: Use this to run any CLI commands, check files, or use Linux tools.
5. MAIL_ACTION: Use this to handle OTPs and real email verification.
   - Use 'create' to get a new address.
   - Use 'get_messages' to check the inbox and find OTP codes.

INSTRUCTIONS:
1. EXPLORATION DEPTH & PIVOT: Your core goal is to verify ALL MISSION SCENARIOS: {self.scenarios}.
   - BE THOROUGH: For scenarios like SECURITY, EDGE_CASE, and VALIDATION_ERROR, do not just try one thing and move on. Attempt at least 2-3 different variations for each scenario on high-impact endpoints.
     * For SECURITY: Try Broken Auth (no token), Malformed Token, and parameter manipulation (IDOR).
     * For EDGE_CASE: Try boundary values (empty, max length, negative numbers, emoji).
     * For VALIDATION: Try missing fields vs malformed fields.
   - PIVOTING: Once a feature has been "stressed" with these variations, pivot to the next scenario or endpoint.
   - CRITICAL: You are NOT ALLOWED to call 'FINISH' until you have actually performed multiple tangiable actions for each mission scenario.
   - OTP/VERIFICATION FLOW: If you initiate an action that sends an email (like signup or password reset), follow these steps:
     1. Use MAIL_ACTION 'create' BEFORE the signup to get a real address.
     2. Use the 'AGENT_EMAIL' variable from your memory in the API/Frontend signup.
     3. Use MAIL_ACTION 'get_messages' to retrieve the OTP.
     4. Proceed with the verification using the 'extracted_otp'.
2. COMPLIANCE CHECKLIST: Before every move, mentally check off which scenarios from {self.scenarios} you have already verified. Do not finish until you have diverse coverage for all of them.
3. SCHEMA OBSESSION: Before calling any API, check its 'request_body' field in the AVAILABLE API ENDPOINTS list. This is your MANDATORY template. Match its keys and casing EXACTLY.
3. STRATEGIZE: If SECURITY is a scenario, look for broken auth or injection points. If EDGE_CASE, try weird values.
4. ADAPT: If an API call fails (4xx/5xx), ANALYZE THE ERROR BODY for the correct keys. 
   - If the server says "FirstName is required", look at your casing! (e.g. maybe it wants 'FirstName' instead of 'firstName').
   - Use the exact keys the server's error message suggests.
4. MISSION COMPLETE: You successfully finish when the intent of the User Story is verified. 
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

            if self.provider == "gemini":
                # ... (Gemini Logic) ...
                url = f"https://generativelanguage.googleapis.com/v1beta/models/{self.model}:generateContent?key={self.gemini_api_key}"
                contents = []
                for msg in messages:
                    role = "user" if msg["role"] in ["user", "system"] else "model"
                    contents.append({"role": role, "parts": [{"text": msg["content"]}]})
                
                payload = {
                    "contents": contents,
                    "generationConfig": {
                        "response_mime_type": "application/json",
                        "temperature": 0.2
                    }
                }
                resp = requests.post(url, json=payload, timeout=30)
                resp.raise_for_status()
                content = resp.json()["candidates"][0]["content"]["parts"][0]["text"]
            else:
                # ... (OpenAI Logic) ...
                url = "https://api.openai.com/v1/chat/completions"
                payload = {
                    "model": self.model,
                    "messages": messages,
                    "temperature": 0.2,
                    "response_format": {"type": "json_object"}
                }
                headers = {"Authorization": f"Bearer {self.openai_api_key}", "Content-Type": "application/json"}
                resp = requests.post(url, json=payload, headers=headers, timeout=30)
                resp.raise_for_status()
                content = resp.json()["choices"][0]["message"]["content"]
            
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
            return {"type": "FINISH", "reason": f"Agent crashed: {e}"}

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

import requests
import json
import logging
import time
from django.conf import settings

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
        # ...
        self.provider = getattr(settings, 'LLM_PROVIDER', 'gemini').lower() 
        self.openai_api_key = getattr(settings, 'LLM_API_KEY', None)
        self.gemini_api_key = getattr(settings, 'GEMINI_API_KEY', None)
        
        if self.provider == "gemini":
            self.model = getattr(settings, 'GEMINI_MODEL', 'gemini-2.5-flash')
        else:
            self.model = getattr(settings, 'OPENAI_MODEL', 'gpt-4o-mini')

        # Agent Memory
        self.session = requests.Session() 
        self.history = [] 
        self.extracted_vars = {} 
        
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
        for e in endpoints:
            logger.info(f"  - {e['method']} {e['url']} ({e['name']})")

        system_prompt = self._build_system_prompt(endpoints)
        self.history.append({"role": "system", "content": system_prompt})
        
        steps_log = []
        
        # 2. The Loop
        correction_count = 0
        for step_i in range(1, max_steps + 1):
            logger.info(f"--- [Step {step_i}/{max_steps}] Thinking ---")
            
            action = self._get_next_action()
            
            # Log the Agent's Decision for visibility
            reason = action.get('reason', 'No reason provided')
            action_type = action.get('type', 'UNKNOWN')
            logger.info(f"[Agent Decision] Type: {action_type} | Reason: {reason}")
            
            if action.get("type") == "FINISH":
                logger.info(f"[Agent] Mission Complete: {action.get('reason')}")
                steps_log.append({
                    "step": step_i,
                    "action": "FINISH",
                    "reason": action.get("reason"),
                    "self_correction_count": correction_count,
                    "status": "success"
                })
                break
                
            if action.get("type") == "ERROR":
                correction_count += 1
                logger.error(f"[Agent] Brain Error: {action.get('reason')}")
                self._record_observation(f"Error from Brain: {action.get('reason')}. Retrying.")
                continue

            if action.get("type") == "BROWSER_ACTION":
                result = self._execute_browser_action(action)
                steps_log.append({
                    "step": step_i, "action": "BROWSER_ACTION",
                    "details": action, "response": result,
                    "status": "passed" if not result.get("error") else "failed"
                })
                self._record_observation(f"Browser Result: {json.dumps(result)}")
                continue

            if action.get("type") == "STRESS_TEST":
                result = self._execute_stress_test(action, endpoint_map)
                steps_log.append({
                    "step": step_i, "action": "STRESS_TEST",
                    "details": action, "response": result,
                    "status": "passed" if not result.get("error") else "failed"
                })
                self._record_observation(f"Stress Test Result: {json.dumps(result)}")
                continue

            if action.get("type") == "CALL_API":
                endpoint_id = action.get("endpoint_id")
                target = endpoint_map.get(endpoint_id)
                
                # Resilient Fallback: If AI sent a URL/Path instead of an ID, try to find it
                if not target:
                    logger.warning(f"[Agent] ID {endpoint_id} not found. Trying URL fallback.")
                    for e_id, e_val in endpoint_map.items():
                        if e_val['url'] == endpoint_id or (endpoint_id and endpoint_id in e_val['url']):
                            target = e_val
                            break
                            
                if not target:
                    self._record_observation(f"Error: Endpoint ID '{endpoint_id}' not found in the mission map. Please use the exact 'id' field from the list.")
                    continue

                logger.info(f"[Agent] Executing {target['method']} {target['url']}")
                req_data = self._prepare_request(action.get("payload", {}))
                
                try:
                    start_time = time.time()
                    resolved_url = self._resolve_url(target['url'], action.get("payload", {}).get("params", {}))
                    response = self.session.request(
                        method=target['method'],
                        url=resolved_url,
                        headers=req_data.get("headers", {}),
                        json=req_data.get("body") if target['method'] in ['POST', 'PUT', 'PATCH'] else None,
                        params=req_data.get("params"),
                        timeout=30
                    )
                    duration = round((time.time() - start_time) * 1000, 2)
                    
                    result_summary = {
                        "status": response.status_code,
                        "reason": response.reason,
                        "body": self._truncate_body(response.text),
                        "duration_ms": duration
                    }
                    
                    is_passed = 200 <= response.status_code < 300
                    if not is_passed:
                        correction_count += 1 # Tracking the self-correction effort
                    
                    steps_log.append({
                        "step": step_i,
                        "action": "CALL_API",
                        "reason": action.get("reason"), # Capture WHY the agent did this
                        "endpoint": target.get('name', 'Unnamed'),
                        "method": target['method'],
                        "url": target['url'],
                        "request": req_data,
                        "response": result_summary,
                        "status": "passed" if is_passed else "failed"
                    })
                    
                    observation = f"API Response: {response.status_code} {response.reason}\nBody: {result_summary['body']}"
                    self._record_observation(observation)
                    
                except Exception as e:
                    correction_count += 1
                    logger.error(f"[Agent] Step failed: {e}")
                    self._record_observation(f"System Error: {str(e)}")
                    steps_log.append({
                        "step": step_i, "action": "ERROR", "error": str(e), "status": "error"
                    })

        return steps_log

    def _execute_browser_action(self, action):
        """Simulates a browser interaction using Playwright and captures visual data."""
        from playwright.sync_api import sync_playwright
        import os
        import base64
        import tempfile

        logger.info(f"[Agent] Browser Action: {action.get('action')} on {action.get('url') or action.get('selector')}")
        
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                page = browser.new_page()
                
                # Execution
                if action.get("action") == "navigate":
                    page.goto(action.get("url"))
                elif action.get("action") == "click":
                    page.click(action.get("selector"))
                elif action.get("action") == "type":
                    page.fill(action.get("selector"), action.get("value"))
                
                # Post-action Wait
                page.wait_for_timeout(2000) 

                # THE VISUAL FEED (Taking the Screenshot)
                screenshot_path = os.path.join(tempfile.gettempdir(), f"agent_sc_{int(time.time())}.png")
                page.screenshot(path=screenshot_path)
                
                # Analyze with Vision
                with open(screenshot_path, "rb") as f:
                    b64_image = base64.b64encode(f.read()).decode('utf-8')
                
                visual_summary = self._analyze_vision(b64_image)
                
                # Metadata
                title = page.title()
                content_preview = page.content()[:500]
                
                browser.close()
                
                # Cleanup
                if os.path.exists(screenshot_path):
                    os.remove(screenshot_path)

                return {
                    "status": "success", 
                    "title": title, 
                    "visual_observation": visual_summary,
                    "html_preview": content_preview
                }
        except Exception as e:
            return {"status": "error", "error": str(e)}

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
        """Executes a simple high-concurrency stress test."""
        import concurrent.futures
        
        target_ids = action.get("endpoints_to_hit", [])
        users = action.get("users", 10)
        
        logger.info(f"[Agent] Stress Test: Hitting {len(target_ids)} endpoints with {users} users.")
        
        def hit_endpoint(endpoint_id):
            target = endpoint_map.get(str(endpoint_id))
            if not target: return None
            try:
                resp = requests.request(target['method'], self._resolve_url(target['url']), timeout=10)
                return resp.status_code
            except:
                return 500

        results = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
            # Simple spread: each user hits all target endpoints once
            futures = [executor.submit(hit_endpoint, eid) for _ in range(users) for eid in target_ids]
            for future in concurrent.futures.as_completed(futures):
                results.append(future.result())
        
        success_count = len([r for r in results if r and r < 400])
        return {
            "total_requests": len(results),
            "success_rate": f"{(success_count/len(results))*100}%" if results else "0%",
            "status": "success"
        }

        return steps_log

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
{"1. CALL_API: Use this for functional backend testing. You MUST provide the 'endpoint_id' from the AVAILABLE API ENDPOINTS list above." if has_http else ""}
{"2. BROWSER_ACTION: Use this to test the UI/UX via Playwright. You have visual capabilities." if has_browser else ""}
{"3. STRESS_TEST: Use this if the user wants to test performance." if has_load else ""}

INSTRUCTIONS:
1. FOCUS & PIVOT: Your core goal is to verify ALL MISSION SCENARIOS: {self.scenarios}. 
   - CRITICAL: You are NOT ALLOWED to call 'FINISH' until you have actually performed at least one CALL_API or BROWSER_ACTION for each mission scenario. 
   - As soon as you get a 2xx success for a "HAPPY_PATH" action, immediately PIVOT to a different scenario (like AUTH_ERROR or SECURITY) for that same feature.
2. COMPLIANCE CHECKLIST: Before every move, mentally check off which scenarios from {self.scenarios} you have already verified. Do not finish until the list is complete.
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

Type D: FINISH
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

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
    def __init__(self, collection, user_story=None, env_vars=None):
        self.collection = collection
        self.user_story = user_story or "Explore the API and ensure core functionality works."
        self.env_vars = env_vars or {}
        
        # LLM Config
        self.provider = getattr(settings, 'LLM_PROVIDER', 'gemini').lower() # Default to Gemini
        self.openai_api_key = getattr(settings, 'LLM_API_KEY', None)
        self.gemini_api_key = getattr(settings, 'GEMINI_API_KEY', None)
        
        # Intelligent Model Selection (2026 Fleet)
        if self.provider == "gemini":
            self.model = getattr(settings, 'GEMINI_MODEL', 'gemini-2.5-flash')
        else:
            self.model = getattr(settings, 'OPENAI_MODEL', 'gpt-4o-mini')

        # Agent Memory (The "Brain")
        self.session = requests.Session() 
        self.history = [] 
        self.extracted_vars = {} 
        
        # Hydrate memory with Env Vars
        if self.env_vars:
            self.extracted_vars.update(self.env_vars)

    def run_mission(self, max_steps=10):
        """
        Executes the agent loop until the user story is satisfied or max_steps reached.
        Returns a list of steps performed with results.
        """
        logger.info(f"[Agent] Starting mission for {self.collection.name}: {self.user_story}")
        
        # 1. Understudy: Initialize Context
        # We now include the schema (request_body, query_params) so the Agent doesn't have to guess keys.
        endpoints = list(self.collection.endpoints.values(
            'id', 'method', 'url', 'name', 'description', 
            'request_body', 'query_params'
        ))
        endpoint_map = {str(e['id']): e for e in endpoints}
        
        system_prompt = self._build_system_prompt(endpoints)
        self.history.append({"role": "system", "content": system_prompt})
        
        steps_log = []
        
        # 2. The Loop
        correction_count = 0
        for step_i in range(1, max_steps + 1):
            logger.info(f"[Agent] Thinking... (Step {step_i}/{max_steps})")
            
            action = self._get_next_action()
            
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
                
                if not target:
                    self._record_observation(f"Error: Endpoint ID {endpoint_id} not found.")
                    continue

                logger.info(f"[Agent] Executing {target['method']} {target['url']}")
                req_data = self._prepare_request(action.get("payload", {}))
                
                try:
                    start_time = time.time()
                    response = self.session.request(
                        method=target['method'],
                        url=self._resolve_url(target['url']),
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
        return f"""
You are an Universal Autonomous QA Agent. You have the "Vibe" of a senior human tester.
Your goal is to verify the User Story: "{self.user_story}"

PROJECT VARS: {json.dumps(self.env_vars)}

AVAILABLE API ENDPOINTS:
{json.dumps(endpoints, indent=2)}

YOUR TOOLSET:
1. CALL_API: Use this for functional backend testing and state preparation.
2. BROWSER_ACTION: Use this to test the UI/UX. You have "EYES" – after every browser action, you will receive a visual description of what the page actually looks like. Use this to verify buttons, error messages, and layouts.
3. STRESS_TEST: Use this if the user wants to test performance.

INSTRUCTIONS:
1. UNDERSTUDY: Look at the whole collection map before your first move.
2. STRATEGIZE: If the story is about "User Experience", start with BROWSER_ACTION.
3. ADAPT: If an API call fails (4xx/5xx), ANALYZE THE ERROR BODY. 
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

    def _resolve_url(self, url_path):
        """Combines base URL with path."""
        # Assuming collection has a base_url, or we rely on the path being absolute
        # For this implementation, we assume the agent receives full paths or we prepend collection base
        if url_path.startswith("http"):
            return url_path
        
        base = self.collection.base_url or ""
        if base and not base.endswith("/"): base += "/"
        return base + url_path.lstrip("/")

    def _truncate_body(self, text, limit=500):
        if len(text) > limit:
            return text[:limit] + "... [truncated]"
        return text

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
            self.model = getattr(settings, 'GEMINI_MODEL', 'gemini-3-flash')
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
        endpoints = list(self.collection.endpoints.values('id', 'method', 'url', 'name', 'description'))
        endpoint_map = {str(e['id']): e for e in endpoints}
        
        system_prompt = self._build_system_prompt(endpoints)
        self.history.append({"role": "system", "content": system_prompt})
        
        steps_log = []
        
        # 2. The Loop
        for step_i in range(1, max_steps + 1):
            logger.info(f"[Agent] Thinking... (Step {step_i}/{max_steps})")
            
            # DECIDE ACTION
            action = self._get_next_action()
            
            if action.get("type") == "BROWSER_ACTION":
                # EXECUTE BROWSER STEP
                result = self._execute_browser_action(action)
                steps_log.append({
                    "step": step_i,
                    "action": "BROWSER_ACTION",
                    "details": action,
                    "response": result,
                    "status": "passed" if not result.get("error") else "failed"
                })
                self._record_observation(f"Browser Result: {json.dumps(result)}")
                continue

            if action.get("type") == "STRESS_TEST":
                # EXECUTE LOAD TEST
                result = self._execute_stress_test(action, endpoint_map)
                steps_log.append({
                    "step": step_i,
                    "action": "STRESS_TEST",
                    "details": action,
                    "response": result,
                    "status": "passed" if not result.get("error") else "failed"
                })
                self._record_observation(f"Stress Test Result: {json.dumps(result)}")
                continue

            if action.get("type") == "CALL_API":
                # EXECUTE API CALL
                endpoint_id = action.get("endpoint_id")
                target = endpoint_map.get(endpoint_id)
                
                if not target:
                    self._record_observation(f"Error: Endpoint ID {endpoint_id} not found in collection map.")
                    continue

                logger.info(f"[Agent] Executing {target['method']} {target['url']}")
                
                # Dynamic Data Injection
                req_data = self._prepare_request(action.get("payload", {}))
                
                # Perform the Request
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
                    
                    # Capture Result
                    result_summary = {
                        "status": response.status_code,
                        "reason": response.reason,
                        "body": self._truncate_body(response.text),
                        "duration_ms": duration
                    }
                    
                    # Log for the User
                    steps_log.append({
                        "step": step_i,
                        "action": "CALL_API",
                        "endpoint": target.get('name', 'Unnamed'),
                        "method": target['method'],
                        "url": target['url'],
                        "request": req_data,
                        "response": result_summary,
                        "status": "passed" if response.status_code < 500 else "failed"
                    })
                    
                    # Feed Observation back to Agent
                    observation = f"API Response: {response.status_code} {response.reason}\nBody: {result_summary['body']}"
                    self._record_observation(observation)
                    
                except Exception as e:
                    logger.error(f"[Agent] Step failed: {e}")
                    self._record_observation(f"System Error Exception: {str(e)}")
                    steps_log.append({
                        "step": step_i,
                        "action": "ERROR",
                        "error": str(e),
                        "status": "error"
                    })

        return steps_log

    def _execute_browser_action(self, action):
        """Simulates a browser interaction using Playwright."""
        from playwright.sync_api import sync_playwright
        logger.info(f"[Agent] Browser Action: {action.get('action')} on {action.get('url') or action.get('selector')}")
        
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                # Use a context to persist state if needed, but for now we do atomic steps
                page = browser.new_page()
                
                if action.get("action") == "navigate":
                    page.goto(action.get("url"))
                elif action.get("click"):
                    page.click(action.get("selector"))
                elif action.get("type"):
                    page.fill(action.get("selector"), action.get("value"))
                
                # Capture a summary of the page state
                content = page.content()[:1000]
                title = page.title()
                browser.close()
                return {"status": "success", "title": title, "page_preview": content}
        except Exception as e:
            return {"status": "error", "error": str(e)}

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
2. BROWSER_ACTION: Use this to test the UI/UX. You can navigate, click, and type via Playwright.
3. STRESS_TEST: Use this if the user wants to test performance or high load. You will define the scenario.

INSTRUCTIONS:
1. UNDERSTUDY: Look at the whole collection map before your first move.
2. STRATEGIZE: If the story is about "User Experience", start with BROWSER_ACTION. If it's "API Reliability", use CALL_API.
3. ADAPT: If an API call fails, don't just die. Think: "Did I miss a header? Do I need to login first?" and fix it.
4. MISSION COMPLETE: Only finish when the story is truly verified across both API and (if needed) UI.

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
        """Calls LLM to decide the next move based on history."""
        try:
            if self.provider == "gemini":
                # NATIVE GEMINI JSON MODE
                url = f"https://generativelanguage.googleapis.com/v1beta/models/{self.model}:generateContent?key={self.gemini_api_key}"
                
                # Convert history to Gemini format (user/model instead of user/assistant)
                contents = []
                for msg in self.history:
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
                # OPENAI FLOW
                url = "https://api.openai.com/v1/chat/completions"
                payload = {
                    "model": self.model,
                    "messages": self.history,
                    "temperature": 0.2,
                    "response_format": {"type": "json_object"}
                }
                headers = {"Authorization": f"Bearer {self.openai_api_key}", "Content-Type": "application/json"}
                resp = requests.post(url, json=payload, headers=headers, timeout=30)
                resp.raise_for_status()
                content = resp.json()["choices"][0]["message"]["content"]
            
            # Append assistant's thought to history
            self.history.append({"role": "assistant", "content": content})
            return json.loads(content)
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

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
        self.provider = getattr(settings, 'LLM_PROVIDER', 'openai').lower()
        self.api_key = getattr(settings, 'LLM_API_KEY', None)
        self.base_url = getattr(settings, 'LLM_BASE_URL', "https://api.openai.com/v1")
        self.model = "gpt-4o-mini" # Fast, smart model for agent loops

        # Agent Memory (The "Brain")
        self.session = requests.Session() # Persist cookies/connection
        self.history = [] # Formatting: [{"role": "user", "content": ...}, ...]
        self.extracted_vars = {} # Dynamic variables (tokens, IDs)
        
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
            
            if action.get("type") == "FINISH":
                logger.info(f"[Agent] Mission Complete: {action.get('reason')}")
                steps_log.append({
                    "step": step_i,
                    "action": "FINISH",
                    "reason": action.get("reason"),
                    "status": "success"
                })
                break
                
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
                        "endpoint": target['name'],
                        "method": target['method'],
                        "url": target['url'],
                        "request": req_data,
                        "response": result_summary,
                        "status": "passed" if response.status_code < 500 else "failed"
                    })
                    
                    # Feed Observation back to Agent
                    observation = f"API Response: {response.status_code} {response.reason}\nBody: {result_summary['body']}"
                    self._record_observation(observation)
                    
                    # Auto-Extract known variables (Naive approach, Agent does sophisticated extraction via thought)
                    # We rely on the Agent to "Observing" use useful values in the next turn
                    
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

    def _build_system_prompt(self, endpoints):
        return f"""
You are an Autonomous QA Agent. Your goal is to verify the User Story on a live API.

USER STORY: "{self.user_story}"
PROJECT VARS: {json.dumps(self.env_vars)}

AVAILABLE ENDPOINTS (Map):
{json.dumps(endpoints, indent=2)}

INSTRUCTIONS:
1. Analyze the endpoints and decide the best next step to fulfill the story.
2. Maintain internal state (IDs, Tokens). If you create a resource, remember its ID.
3. If an API fails (400/401/404), ANALYZE WHY and correct yourself in the next step (e.g. "I forgot auth", "I need to create a user first").
4. Repeat until the story is fully verified or you are stuck.

OUTPUT FORMAT (JSON ONLY):
Return a JSON object with ONE of these types:

Type A: CALL_API
{{
  "type": "CALL_API",
  "endpoint_id": "...",
  "reason": "I need to login to get a token.",
  "payload": {{
    "headers": {{ "Authorization": "Bearer ..." }},
    "body": {{ ... }},
    "params": {{ ... }}
  }}
}}

Type B: FINISH
{{
  "type": "FINISH",
  "reason": "I have successfully created and deleted the project. The story is verified."
}}
"""

    def _get_next_action(self):
        """Calls LLM to decide the next move based on history."""
        try:
            payload = {
                "model": self.model,
                "messages": self.history,
                "temperature": 0.2, # Low temp for precise actions
                "response_format": {"type": "json_object"}
            }
            
            headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
            resp = requests.post(f"{self.base_url}/chat/completions", json=payload, headers=headers, timeout=30)
            resp.raise_for_status()
            
            content = resp.json()["choices"][0]["message"]["content"]
            # Append assistant's thought to history (so it remembers its own plan)
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

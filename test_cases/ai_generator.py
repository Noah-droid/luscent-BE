import requests
import json
import os
import logging
from django.conf import settings

logger = logging.getLogger(__name__)

class AITestGenerator:
    def __init__(self):
        # Configurable via settings
        self.provider = getattr(settings, 'LLM_PROVIDER', 'openai').lower()
        self.openai_api_key = getattr(settings, 'LLM_API_KEY', None)
        self.openai_base_url = getattr(settings, 'LLM_BASE_URL', "https://api.openai.com/v1")
        self.gemini_api_key = getattr(settings, 'GEMINI_API_KEY', None)
        
        # Using latest 2026 state-of-the-art models
        if self.provider == "gemini":
            self.model = getattr(settings, 'GEMINI_MODEL', 'gemini-3-flash')
        else:
            self.model = getattr(settings, 'OPENAI_MODEL', 'gpt-4o-mini')
            
        self.max_tokens = 8192 
        self.temperature = 0.5 # Lower temperature for better structural planning

    def orchestrate_collection_tests(self, collection, user_story=None, scenarios=None, allowed_runners=None):
        """
        New Main Entry Point: Generates an orchestrated sequence of tests for a collection.
        Instead of isolated endpoints, it creates a 'Success Path' flow.
        """
        if not allowed_runners:
            allowed_runners = ["http"]

        # 0. Get Project Environment Variables
        project_vars = {}
        if hasattr(collection.project, 'environment_variables') and collection.project.environment_variables:
             project_vars = collection.project.environment_variables

        # 1. Gather all endpoints in the collection
        endpoints = collection.endpoints.all()
        endpoint_map = [
            {
                "id": str(e.id),
                "method": e.method,
                "url": e.url,
                "name": e.name,
                "description": e.description,
                "request_body": e.request_body,
                "query_params": e.query_params
            } for e in endpoints
        ]

        if not endpoint_map:
            return []

        # 2. Step 1: PLAN THE FLOW (The Architect)
        plan = self._plan_orchestration(collection, endpoint_map, user_story, scenarios)
        if not plan:
            logger.warning("AI failed to generate an orchestration plan.")
            return []

        logger.info(f"[Orchestrator] Generated plan with {len(plan)} steps.")

        # 3. Step 2: GENERATE THE ACTUAL TESTS (The Script Writer)
        orchestrated_tests = []
        for step in plan:
            # Find the actual endpoint object
            target_endpoint = endpoints.filter(id=step.get("endpoint_id")).first()
            if not target_endpoint:
                continue

            test_data = self._generate_stateful_test(target_endpoint, step, collection, user_story, allowed_runners, project_vars)
            if test_data:
                orchestrated_tests.extend(test_data)

        return orchestrated_tests

    def _plan_orchestration(self, collection, endpoint_map, user_story, scenarios, project_vars=None):
        """
        Analyzes the collection and returns a list of logical steps with dependencies.
        """
        env_context = f"\nPROJECT ENVIRONMENT VARIABLES:\n{json.dumps(project_vars or {}, indent=2)}" if project_vars else ""
        
        prompt = f"""
You are a Senior QA Architect. Your task is to design a SENTIENT execution plan for this API collection.
Do NOT just loop through endpoints. Instead, follow the "User Lifecycle".

PROJECT: {collection.project.name}
COLLECTION: {collection.name}
GOAL: {user_story or 'Establish a full working environment and test all core features.'}
{env_context}

ENDPOINTS AVAILABLE:
{json.dumps(endpoint_map, indent=2)}

TASK:
1. PHASE 1: DISCOVERY - Identify the "Entry Point" endpoints (Sign up, Login, Auth, or Onboarding). These must run FIRST.
2. PHASE 2: RESOURCE CREATION - Identify which endpoints create the core resources (e.g., POST /projects, POST /users).
3. PHASE 3: INTERACTION - Identify endpoints that use those resources (GET, PUT, PATCH).
4. PHASE 4: CLEANUP - Identify DELETE endpoints (These must run LAST).

LOGICAL FLOW RULES:
- If an endpoint needs 'project_id', it MUST be preceded by an endpoint that provides it.
- If the collection has Authentication, the very first step MUST be obtaining a token.
- GROUP tests into a single logical "Success Path" story.

OUTPUT FORMAT (Strict JSON List):
[
  {{
    "endpoint_id": "UUID",
    "name": "Step 1: Authenticate",
    "purpose": "Identify as a valid user to enable further testing.",
    "extract_vars": {{"auth_token": "$.token"}},
    "depends_on_vars": []
  }},
  ...
]
"""
        try:
            response = self._call_llm(prompt)
            plan = self._parse_response(response)
            return plan
        except Exception as e:
            logger.error(f"Planning failed: {e}")
            return None

    def _generate_stateful_test(self, endpoint, step_plan, collection, user_story, allowed_runners, project_vars=None):
        """
        Generates the actual TestCase data for a specific step in the plan.
        """
        env_context = f"\nPROJECT ENVIRONMENT VARIABLES (Use these where appropriate):\n{json.dumps(project_vars or {}, indent=2)}" if project_vars else ""

        prompt = f"""
You are a Lead QA Engineer. Generate the detailed test configuration for a specific step in a planned flow.

COLLECTION CONTEXT: {collection.name}
GLOBAL STORY: {user_story}
{env_context}

STEP PLAN:
{json.dumps(step_plan, indent=2)}

ACTIVE ENDPOINT:
- Method: {endpoint.method}
- URL: {endpoint.url}
- Request Schema: {json.dumps(endpoint.request_body)}

DYNAMIC DATA INSTRUCTIONS:
1. USE VARIABLES: If this step depends on '{{{{var_name}}}}', use that syntax in the URL, headers, or body.
2. EXTRACT DATA: Always add an 'extract' type assertion for variables defined in the step plan.
   Example: {{"type": "extract", "field": "$.id", "variable": "project_id"}}
3. RUNNER: {json.dumps(allowed_runners)}

OUTPUT: Provide a JSON list containing 1-2 detailed test cases. 
Follow the standard TestCase schema:
{{
    "name": "...",
    "test_script": "...",
    "headers": {{}},
    "body": {{}},
    "assertions": [
        {{"type": "status", "value": 200}},
        {{"type": "extract", "field": "...", "variable": "..."}}
    ]
}}
"""
        try:
            response = self._call_llm(prompt)
            return self._parse_response(response)
        except Exception as e:
            logger.error(f"Step generation failed for {endpoint.name}: {e}")
            return []

    def _call_llm(self, prompt):
        if self.provider == "gemini":
            return self._call_gemini(prompt)
        else:
            return self._call_openai(prompt)

    def _call_openai(self, prompt):
        headers = {
            "Authorization": f"Bearer {self.openai_api_key}",
            "Content-Type": "application/json"
        }
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": "You are a professional QA Orchestrator. Output ONLY valid JSON."},
                {"role": "user", "content": prompt}
            ],
            "temperature": self.temperature
        }
        resp = requests.post(f"{self.openai_base_url}/chat/completions", json=payload, headers=headers, timeout=40)
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"]

    def _call_gemini(self, prompt):
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{self.model}:generateContent?key={self.gemini_api_key}"
        headers = {"Content-Type": "application/json"}
        payload = {
            "contents": [{
                "parts": [{"text": f"SYSTEM: You are a professional QA Orchestrator. Output ONLY valid JSON.\n\nUSER: {prompt}"}]
            }],
            "generationConfig": {
                "temperature": self.temperature,
                "maxOutputTokens": self.max_tokens,
                "response_mime_type": "application/json"
            }
        }
        resp = requests.post(url, json=payload, headers=headers, timeout=40)
        resp.raise_for_status()
        return resp.json()["candidates"][0]["content"]["parts"][0]["text"]

    def _parse_response(self, response):
        """Cleans AI wrapping and parses JSON."""
        try:
            clean_json = response.strip()
            if clean_json.startswith("```json"):
                clean_json = clean_json[7:]
            if clean_json.endswith("```"):
                clean_json = clean_json[:-3]
            return json.loads(clean_json.strip())
        except Exception as e:
            logger.error(f"Failed to parse AI response: {e}\nResponse: {response}")
            return []

    def analyze_screenshot(self, image_path):
        """Analyze a screenshot for visual defects using Vision AI."""
        import base64
        if not os.path.exists(image_path): return "Error: Screenshot not found."
        with open(image_path, "rb") as image_file:
            encoded_string = base64.b64encode(image_file.read()).decode('utf-8')
        
        prompt = "Describe visual glitches, error messages, or layout breaks in this app. If perfect, reply 'NO_DEFECTS'."
        try:
            if self.provider == "gemini":
                return self._call_gemini_vision(prompt, encoded_string)
            return self._call_openai_vision(prompt, encoded_string)
        except Exception as e:
            logger.error(f"Visual Analysis failed: {e}")
            return f"Analysis Error: {e}"

    def _call_openai_vision(self, prompt, base64_image):
        headers = {"Authorization": f"Bearer {self.openai_api_key}", "Content-Type": "application/json"}
        payload = {
            "model": "gpt-4o-mini",
            "messages": [{"role": "user", "content": [{"type": "text", "text": prompt}, {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{base64_image}"}}]}],
            "max_tokens": 300
        }
        resp = requests.post(f"{self.openai_base_url}/chat/completions", json=payload, headers=headers, timeout=40)
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"]

    def _call_gemini_vision(self, prompt, base64_image):
        url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash:generateContent?key={self.gemini_api_key}"
        payload = {"contents": [{"parts": [{"text": prompt}, {"inline_data": {"mime_type": "image/png", "data": base64_image}}]}]}
        resp = requests.post(url, json=payload, headers={"Content-Type": "application/json"}, timeout=40)
        resp.raise_for_status()
        return resp.json()["candidates"][0]["content"]["parts"][0]["text"]

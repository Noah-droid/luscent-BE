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
        
        self.model = "gpt-4o-mini" if self.provider == "openai" else "gemini-2.5-flash"
        self.max_tokens = 8192 
        self.temperature = 0.7

    def generate_draft_plan(self, endpoint_item, allowed_runners=None, category="functional", layer="backend", scenarios=None, project_description="", user_story=None):
        """
        Generate test case drafts for a given Endpoint (url/method) object.
        Returns a list of dicts suitable for creating TestCase objects.
        """
        if allowed_runners is None:
            allowed_runners = ["http"]

        api_key = self.openai_api_key if self.provider == "openai" else self.gemini_api_key
        
        if not api_key or api_key == "PLACEHOLDER":
            logger.warning(f"No API key found for provider {self.provider}. Skipping AI generation.")
            return []

        prompt = self._construct_prompt(endpoint_item, allowed_runners, category, layer, scenarios, project_description, user_story)
        
        try:
            response = self._call_llm(prompt)
            test_cases_data = self._parse_response(response)
            # Inject user_story into the result so it can be saved later
            if user_story:
                for test in test_cases_data:
                    test['user_story'] = user_story
            return test_cases_data
        except Exception as e:
            logger.error(f"AI Generation failed ({self.provider}): {e}")
            return []

    # ... (refine_test method remains unchanged) ...

    def _construct_prompt(self, item, allowed_runners, category, layer, scenarios, project_description="", user_story=None):
        # Build context about the endpoint
        context = {
            "method": item.method,
            "url": item.url,
            "description": item.description,
            "request_body_schema": item.request_body,
            "query_params": item.query_params,
        }
        
        # Inject Project Variables if available
        project_vars = {}
        if hasattr(item.collection.project, 'environment_variables') and item.collection.project.environment_variables:
             project_vars = item.collection.project.environment_variables

        # **NEW: Gather Collection Context for Smarter AI**
        collection_context = self._gather_collection_context(item.collection)

        # Format requested scenarios for the prompt
        scenario_instruction = ""
        
        # Handle User Story Context (Highest Priority)
        story_instruction = ""
        if user_story:
            story_instruction = f"""
            CRITICAL - USER STORY / REQUIREMENTS:
            The user has provided specific requirements/story for this test generation.
            You MUST PRIORITIZE these requirements over generic scenarios.
            
            User Story:
            "{user_story}"
            
            Interpret this story (even if in plain English or Gherkin) and generate tests that specifically verify these requirements.
            """

        if scenarios and isinstance(scenarios, list):
            scenario_list = ", ".join(scenarios)
            scenario_instruction = f"""
            STRICT REQUIREMENT: You must ONLY generate test cases for the following scenarios:
            {scenario_list}
            
            Do NOT generate any other types of tests.
            
            SCENARIO GUIDELINES:
            - If 'SECURITY' is listed:
              1. Generate tests for SQL Injection, XSS, and Broken Access Control.
              2. CRITICAL: If the endpoint accepts a text prompt, chat message, or user query (AI Endpoint), you MUST generate **Prompt Injection** tests (e.g., 'Ignore previous instructions', 'DAN Mode', 'Leak System Prompt').
              3. Check for PII leaks in responses.
            - If 'HAPPY_PATH' is listed: Focus on 200/201 responses with valid data.
            - If 'VALIDATION_ERROR' is listed: Focus on 400 responses with missing/invalid fields.
            """
        else:
            scenario_instruction = "Generate 3 diverse test cases covering happy paths and common error validation."

        runner_instruction = f"""
        AVAILABLE RUNNERS: {json.dumps(allowed_runners)}
        
        Decide the best 'runner_type' for each test case from the available runners.
        - Use 'browser': ONLY if the endpoint serves HTML, requires a UI flow, or if testing frontend interaction is critical.
        - Use 'http': For standard API logic, JSON responses, and backend validation.
        - Use 'load': ONLY if the category is 'performance' or 'load'.
        """
        
        if "browser" in allowed_runners:
             runner_instruction += """
        For 'browser' tests, you MUST generate a valid Python Playwright script in the 'test_script' field.
        The script should visit the page (if applicable) or perform the action.
        CRITICAL: The runner provides a path in os.environ['SCREENSHOT_PATH']. 
        If the test fails OR if you want to capture state, you MUST save a screenshot to this path using:
        `page.screenshot(path=os.environ['SCREENSHOT_PATH'])`
        """

        if "load" in allowed_runners:
            runner_instruction += """
        For 'load' tests, generate a Python Locust task in 'test_script' field.
        """

        runner_instruction += """
        For 'http' tests, focus on 'headers', 'query_params', 'body' and 'assertions'.
        """

        instructions = f"""
        You are an expert QA Automation Engineer. 
        Generate test cases for the following API endpoint/feature.

        Context:
        - Project: {project_description or 'N/A'}
        - Type: {category} Testing
        - Layer: {layer}
        
        {runner_instruction}
        
        {story_instruction}
        
        {scenario_instruction}
        
        Project Environment Variables (Use these values for test data where applicable):
        {json.dumps(project_vars, indent=2)}

        Endpoint Details:
        {json.dumps(context, indent=2)}
        """

        # **NEW: Add Collection Context for Few-Shot Learning**
        if collection_context['has_examples']:
            instructions += f"""
        
        COLLECTION CONTEXT (Learn from these patterns):
        This endpoint is part of the "{item.collection.name}" collection.
        Collection Description: {item.collection.description or 'No description provided.'}
        
        Previous tests in this collection show these patterns:
        - Auth Type: {collection_context['auth_type']}
        - Common Headers: {json.dumps(collection_context['common_headers'], indent=2)}
        - Response Format: {collection_context['response_format']}
        - Common Assertions: {json.dumps(collection_context['common_assertions'][:3], indent=2)}
        
        Example tests from this collection (FOLLOW THIS STYLE):
        {collection_context['example_tests']}
        
        IMPORTANT: Generate tests that match the style and patterns shown above.
        Use similar assertion types, header structures, and naming conventions.
        """

        instructions += """
        Output strictly valid JSON list of objects. No markdown. No comments.
        Use double quotes for all keys and strings.
        
        CRITICAL: Include a 'steps_summary' field with a human-readable list of steps for non-technical users to review.
        
        Format:
        [
        {
            "name": "Test Name",
            "description": "What this tests",
            "steps_summary": "1. Step one. 2. Step two. 3. Expected Result.",
            "runner_type": "CHOSEN_RUNNER_TYPE",
            "category": "{category}",
            "layer": "{layer}",
            "priority": "high/medium/low",
            "headers": {},
            "query_params": {},
            "body": {},
            "expected_status": 200,
            "assertions": [
            {"type": "status", "value": 200},
            {"type": "json_path", "field": "$.id", "operator": "exists"}
            ],
            "tags": ["SCENARIO:HAPPY_PATH"],
            "test_script": null,
            "use_visual_ai": false
        }
        ]
        """
        return instructions

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
                {"role": "system", "content": "You are a helpful QA assistant that outputs only JSON."},
                {"role": "user", "content": prompt}
            ],
            "temperature": self.temperature
        }

        resp = requests.post(f"{self.openai_base_url}/chat/completions", json=payload, headers=headers, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        return data["choices"][0]["message"]["content"]

    def _call_gemini(self, prompt):
        # Native Gemini REST API
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{self.model}:generateContent?key={self.gemini_api_key}"
        
        headers = {
            "Content-Type": "application/json"
        }
        
        payload = {
            "contents": [{
                "parts": [{"text": prompt}]
            }],
            "generationConfig": {
                "temperature": self.temperature,
                "maxOutputTokens": self.max_tokens,
                "response_mime_type": "application/json"
            }
        }

        resp = requests.post(url, json=payload, headers=headers, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        
        # Extract content from Gemini response
        try:
            content = data["candidates"][0]["content"]["parts"][0]["text"]
            return content
        except (KeyError, IndexError) as e:
            logger.error(f"Failed to parse Gemini response: {data}")
            raise Exception("Invalid Gemini response format")

    def _parse_response(self, content):
        # AI might wrap in ```json ... ```
        clean_content = content.replace("```json", "").replace("```", "").strip()
        
        try:
            return json.loads(clean_content)
        except json.JSONDecodeError:
            # cleanup for errors
            # 1. Trailing commas
            # 2. Single quotes instead of double (risky but common)
            try:
                
                import re
                clean_content = re.sub(r',\s*([\]}])', r'\1', clean_content)
                return json.loads(clean_content)
            except:
                logger.error(f"Failed to parse JSON content: {clean_content}")
                raise

    def analyze_screenshot(self, image_path):
        """
        Analyzes a screenshot for visual defects using Vision AI.
        Returns a string description of findings.
        """
        import base64
        
        if not os.path.exists(image_path):
            return "Error: Screenshot not found."
            
        with open(image_path, "rb") as image_file:
            encoded_string = base64.b64encode(image_file.read()).decode('utf-8')

        prompt = """
        You are a QA Visual Inspector. Analyze this screenshot of a web application.
        Look for:
        1. Visual Glitches (broken layout, overlapping text)
        2. Error Messages (toasts, red alerts, stack traces)
        3. Broken Images
        
        If it looks correct, reply exactly: "NO_DEFECTS".
        If there are issues, describe them briefly.
        """

        try:
            if self.provider == "gemini":
                return self._call_gemini_vision(prompt, encoded_string)
            else:
                return self._call_openai_vision(prompt, encoded_string)
        except Exception as e:
            logger.error(f"Visual Analysis failed: {e}")
            return f"Visual Analysis Error: {e}"

    def _call_openai_vision(self, prompt, base64_image):
        if self.model == "gpt-3.5-turbo":
             # Fallback if using older model setting
             self.model = "gpt-4o-mini"
             
        headers = {
            "Authorization": f"Bearer {self.openai_api_key}",
            "Content-Type": "application/json"
        }
        
        payload = {
            "model": self.model,
            "messages": [
                {
                    "role": "user", 
                    "content": [
                        {"type": "text", "text": prompt},
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:image/png;base64,{base64_image}"
                            }
                        }
                    ]
                }
            ],
            "max_tokens": 300
        }
        
        resp = requests.post(f"{self.openai_base_url}/chat/completions", json=payload, headers=headers, timeout=30)
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"]

    def _call_gemini_vision(self, prompt, base64_image):
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{self.model}:generateContent?key={self.gemini_api_key}"
        
        headers = {"Content-Type": "application/json"}
        
        payload = {
            "contents": [{
                "parts": [
                    {"text": prompt},
                    {
                        "inline_data": {
                            "mime_type": "image/png",
                            "data": base64_image
                        }
                    }
                ]
            }]
        }

        resp = requests.post(url, json=payload, headers=headers, timeout=30)
        resp.raise_for_status()
        
        try:
            return resp.json()["candidates"][0]["content"]["parts"][0]["text"]
        except:
            return "Error parsing Gemini vision response"

    def _gather_collection_context(self, collection):
        """
        Gather context from previous tests in the collection for few-shot learning.
        Returns patterns, examples, and insights to make AI smarter.
        """
        from .models import TestCase, TestRun
        
        # Get recent tests in this collection (limit to 10 for performance)
        recent_tests = TestCase.objects.filter(
            endpoint__collection=collection
        ).order_by('-created_at')[:10]
        
        if not recent_tests.exists():
            return {
                'has_examples': False,
                'auth_type': 'none',
                'common_headers': {},
                'response_format': 'unknown',
                'common_assertions': [],
                'example_tests': ''
            }
        
        # Extract patterns
        auth_types = set()
        all_headers = []
        all_assertions = []
        response_formats = []
        
        for test in recent_tests:
            # Collect auth types from endpoints
            if hasattr(test.endpoint, 'auth_type'):
                auth_types.add(test.endpoint.auth_type)
            
            # Collect headers
            if test.headers:
                all_headers.append(test.headers)
            
            # Collect assertions
            if test.assertions:
                all_assertions.extend(test.assertions)
            
            # Infer response format from assertions
            for assertion in (test.assertions or []):
                if assertion.get('type') == 'json_path':
                    response_formats.append('JSON')
                elif assertion.get('type') == 'contains':
                    response_formats.append('HTML/Text')
        
        # Determine most common auth type
        auth_type = list(auth_types)[0] if auth_types else 'none'
        
        # Find common headers (headers that appear in multiple tests)
        common_headers = self._extract_common_headers(all_headers)
        
        # Determine response format
        response_format = max(set(response_formats), key=response_formats.count) if response_formats else 'JSON'
        
        # Get most common assertion patterns
        common_assertions = self._extract_common_assertions(all_assertions)
        
        # Format example tests (top 3 for few-shot learning)
        example_tests = self._format_example_tests(recent_tests[:3])
        
        return {
            'has_examples': True,
            'auth_type': auth_type,
            'common_headers': common_headers,
            'response_format': response_format,
            'common_assertions': common_assertions,
            'example_tests': example_tests
        }
    
    def _extract_common_headers(self, all_headers):
        """Find headers that appear in multiple tests."""
        if not all_headers:
            return {}
        
        # Count header occurrences
        header_counts = {}
        for headers in all_headers:
            for key, value in headers.items():
                if key not in header_counts:
                    header_counts[key] = []
                header_counts[key].append(value)
        
        # Return headers that appear in at least 2 tests
        common = {}
        for key, values in header_counts.items():
            if len(values) >= 2:
                # Use most common value
                common[key] = max(set(values), key=values.count)
        
        return common
    
    def _extract_common_assertions(self, all_assertions):
        """Find most common assertion patterns."""
        if not all_assertions:
            return []
        
        # Group by type
        assertion_types = {}
        for assertion in all_assertions:
            atype = assertion.get('type', 'unknown')
            if atype not in assertion_types:
                assertion_types[atype] = []
            assertion_types[atype].append(assertion)
        
        # Return most common ones (max 5)
        common = []
        for atype, assertions in sorted(assertion_types.items(), key=lambda x: len(x[1]), reverse=True)[:5]:
            # Pick a representative example
            common.append(assertions[0])
        
        return common
    
    def _format_example_tests(self, tests):
        """Format tests as examples for the AI prompt."""
        if not tests:
            return "No previous tests available."
        
        examples = []
        for i, test in enumerate(tests, 1):
            example = f"""
Example {i}: {test.name}
- Description: {test.description or 'N/A'}
- Priority: {test.priority}
- Headers: {json.dumps(test.headers or {}, indent=2)}
- Body: {json.dumps(test.body or {}, indent=2) if test.body else 'None'}
- Expected Status: {test.expected_status}
- Assertions: {json.dumps(test.assertions[:2] if test.assertions else [], indent=2)}
"""
            examples.append(example.strip())
        
        return "\n\n".join(examples)



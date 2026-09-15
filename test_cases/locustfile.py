from locust import HttpUser, task, events, constant
import json
import os

class APIUser(HttpUser):
    _credentials = []
    _cred_index = 0

    @task
    def run_test(self):
        endpoint_data = self.environment.parsed_options.endpoint_data
        if not endpoint_data:
            return

        method = endpoint_data.get("method", "GET")
        url = endpoint_data.get("url")
        headers = endpoint_data.get("headers", {})
        body = endpoint_data.get("body", {})

        # Inject auth token if available
        if self._credentials:
            cred = self._credentials[APIUser._cred_index % len(self._credentials)]
            token = cred if isinstance(cred, str) else cred.get("token", "")
            if token:
                headers["Authorization"] = f"Bearer {token}"
            APIUser._cred_index += 1

        with self.client.request(
            method=method,
            url=url,
            headers=headers,
            json=body,
            catch_response=True
        ) as response:
            if response.status_code == endpoint_data.get("expected_status", 200):
                response.success()
            else:
                response.failure(f"Expected {endpoint_data.get('expected_status')} got {response.status_code}")

@events.init_command_line_parser.add_listener
def _(parser):
    parser.add_argument("--endpoint-data", type=json.loads, default="{}", help="Endpoint configuration")
    parser.add_argument("--think-time", type=float, default=0, help="Delay between requests per user (ms)")
    parser.add_argument("--base-url", type=str, default="http://localhost", help="Target API base URL")

@events.init.add_listener
def on_init(environment, **kwargs):
    think_time = getattr(environment.parsed_options, 'think_time', 0) or 0
    if think_time > 0:
        environment.user_classes[0].wait_time = constant(think_time / 1000.0)
    
    base_url = getattr(environment.parsed_options, 'base_url', 'http://localhost')
    if base_url:
        environment.user_classes[0].host = base_url.rstrip('/')
    
    # Load credentials if available
    cred_path = "/home/user/credentials.json"
    if os.path.exists(cred_path):
        with open(cred_path) as f:
            APIUser._credentials = json.load(f)

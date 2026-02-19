from locust import HttpUser, task, between, events, constant
import json
import logging

class APIUser(HttpUser):
    wait_time = constant(1)
    host = "http://localhost" # Dummy host to satisfy Locust validation

    @task
    def run_test(self):
        # These will be monkey-patched by the runner before execution
        endpoint_data = self.environment.parsed_options.endpoint_data
        if not endpoint_data:
            return

        method = endpoint_data.get("method", "GET")
        url = endpoint_data.get("url")
        headers = endpoint_data.get("headers", {})
        body = endpoint_data.get("body", {})

        # Locust request
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

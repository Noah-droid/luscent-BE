import json
import yaml
import logging
from urllib.parse import urljoin
import requests
from openapi_spec_validator import validate_spec



logger = logging.getLogger(__name__)

# Map OpenAPI operation to our method choices
def normalize_method(method):
    return method.upper()

def load_spec_from_text(text):
    """
    Try JSON then YAML parse.
    Returns dict or raises ValueError.
    """
    try:
        return json.loads(text)
    except Exception:
        try:
            return yaml.safe_load(text)
        except Exception as e:
            raise ValueError("Failed to parse spec as JSON or YAML.") from e


def fetch_spec_from_url(url, timeout=10):
    resp = requests.get(url, timeout=timeout)
    resp.raise_for_status()
    return resp.text


def validate_openapi(spec_dict):
    try:
        validate_spec(spec_dict)
        return True, None
    except Exception as e:
        # Catching everything because openapi_spec_validator exceptions 
        # hierarchy is inconsistent across versions.
        return False, str(e)



def extract_base_url(spec: dict, fallback: str | None = None):
    """
    Attempt to derive a base URL from servers (OpenAPI v3) or schemes/host/basePath (v2).
    """
    if not spec:
        return fallback
    # OpenAPI v3
    servers = spec.get("servers")
    if servers and isinstance(servers, list):
        first = servers[0].get("url")
        if first:
            return first
    # Swagger v2
    host = spec.get("host")
    schemes = spec.get("schemes")
    base_path = spec.get("basePath", "")
    if host:
        scheme = schemes[0] if schemes else "https"
        return f"{scheme}://{host}{base_path}"
    return fallback


def parse_paths_to_endpoints(spec: dict, project_obj=None, default_base_url=None):
    """
    Walk `paths` and convert into endpoint dicts ready to be saved.
    Returns tuple (imported_count, skipped_count, errors_list, endpoints_list)
    endpoints_list = [{method, path, summary, parameters, requestBody, responses, security}]
    """
    results = {"imported": 0, "skipped": 0, "errors": [], "endpoints": []}
    if not spec:
        results["errors"].append("Empty spec.")
        return results

    paths = spec.get("paths", {})
    if not paths:
        results["errors"].append("No paths found in spec.")
        return results

    # Server/base_url resolution
    base_url = extract_base_url(spec, fallback=default_base_url)

    for raw_path, operations in paths.items():
        if not isinstance(operations, dict):
            results["errors"].append(f"Unexpected operations structure for path {raw_path}")
            continue

        for method, op_obj in operations.items():
            try:
                m = normalize_method(method)
                # skip summary-only or vendor extensions
                if m not in {"GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"}:
                    results["skipped"] += 1
                    continue

                # Ensure base_url has trailing slash for urljoin to work correctly with paths
                effective_base = base_url or ""
                if effective_base and not effective_base.endswith("/"):
                    effective_base += "/"

                endpoint = {
                    "method": m,
                    "path": raw_path,
                    "full_url": urljoin(effective_base, raw_path.lstrip("/")),
                    "name": op_obj.get("summary") or op_obj.get("operationId") or f"{m} {raw_path}",
                    "description": op_obj.get("description", ""),
                    "parameters": op_obj.get("parameters", []),
                    "requestBody": op_obj.get("requestBody"),
                    "responses": op_obj.get("responses", {}),
                    "security": op_obj.get("security", spec.get("security", [])),
                }
                results["endpoints"].append(endpoint)
                results["imported"] += 1
            except Exception as e:
                logger.exception("Failed to parse operation %s %s", method, raw_path)
                results["errors"].append(f"Failed to parse {method} {raw_path}: {str(e)}")

    return results



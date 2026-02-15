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
        res = json.loads(text)
        if not isinstance(res, dict):
             raise ValueError("Parsed JSON is not a dictionary.")
        return res
    except Exception:
        try:
            res = yaml.safe_load(text)
            if not isinstance(res, dict):
                 raise ValueError("Parsed YAML is not a dictionary (likely HTML or plain text).")
            return res
        except Exception as e:
            raise ValueError("Failed to parse spec as JSON or YAML. Ensure you are providing the raw JSON/YAML endpoint, not the Swagger UI HTML page.") from e


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

                # Improved Schema Extraction with Ref Resolution
                request_body = {}
                
                def resolve_schema(s):
                    if not isinstance(s, dict): return {}
                    if "$ref" in s:
                        ref_path = s["$ref"].split("/")
                        ref_key = ref_path[-1]
                        # Look in definitions (Swagger 2) or components/schemas (OpenAPI 3)
                        definitions = spec.get("definitions", {}) or spec.get("components", {}).get("schemas", {})
                        return resolve_schema(definitions.get(ref_key, {}))
                    return s.get("properties") or s.get("example") or s

                rb_obj = op_obj.get("requestBody")
                if rb_obj and isinstance(rb_obj, dict):
                    content = rb_obj.get("content", {})
                    for _, media_type in content.items():
                        schema_obj = media_type.get("schema", {})
                        if schema_obj:
                            request_body = resolve_schema(schema_obj)
                            break
                
                # Fallback and Parameter Flattening (for Swagger 2 or Query Params)
                flattened_query = {}
                params = op_obj.get("parameters", [])
                for p in params:
                    if not isinstance(p, dict): continue
                    p_name = p.get("name")
                    p_in = p.get("in")
                    
                    if p_in == "body" and not request_body:
                        request_body = resolve_schema(p.get("schema", {}))
                    elif p_in == "formData":
                        request_body[p_name] = p.get("type", "string")
                    elif p_in == "query":
                        flattened_query[p_name] = p.get("type", "string")

                endpoint = {
                    "method": m,
                    "path": raw_path,
                    "full_url": urljoin(effective_base, raw_path.lstrip("/")),
                    "name": op_obj.get("summary") or op_obj.get("operationId") or f"{m} {raw_path}",
                    "description": op_obj.get("description", ""),
                    "parameters": op_obj.get("parameters", []),
                    "requestBody": rb_obj, # keep raw for full context
                    "flattened_body": request_body, # Helper for the AI
                    "flattened_query": flattened_query,
                    "responses": op_obj.get("responses", {}),
                    "security": op_obj.get("security", spec.get("security", [])),
                }
                results["endpoints"].append(endpoint)
                results["imported"] += 1
            except Exception as e:
                logger.exception("Failed to parse operation %s %s", method, raw_path)
                results["errors"].append(f"Failed to parse {method} {raw_path}: {str(e)}")

    return results



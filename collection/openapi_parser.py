import json
import logging
import re
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urljoin
from urllib.parse import urlparse

import requests
import yaml
from openapi_spec_validator import validate_spec

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

SWAGGER_UI_SPECS = (
    "doc.json",
    "swagger.json",
    "openapi.json",
    "swagger.yaml",
    "openapi.yaml",
)

_SWAGGER_UI_VAR_RE = re.compile(
    r"""(?:url|configUrl|spec)\s*[:=]\s*["']?\s*(https?://[^"'\s<>]+|/[^"'\s<>]+)["']?""",
    re.IGNORECASE | re.DOTALL,
)


def _resolve_ui_candidate_urls(html_url: str) -> list[str]:
    """
    Resolve probable spec-URL candidates for a Swagger UI HTML page whose
    address is ``html_url``.

    Two sources of candidates, tried in order:

    1. *Relative path heuristics.* The HTML page itself often does not contain
       the spec URL in an easily-parseable way (the initializer JS may point at
       a relative path like ``doc.json``, which ``urljoin(html_url, "doc.json)``
       resolves against the *HTML file*, not the *HTML page*).  The most common
       convention for trivially-hosted Swagger UI is a sibling file, so we try
       the page's own directory (via the ``/``-appended variant of the HTML URL
       so that ``urljoin`` treats the page as a directory) plus the parent
       directory when the page lives in a sub-path.

    2. *Inline config extraction.* Some Swagger UI pages embed the spec URL as
       a JavaScript string literal (e.g. ``url: "doc.json"``).  This is a
       fallback — fewer requests but more fragile across Swagger UI variants.
    """
    parsed = urlparse(html_url)
    candidates: list[str] = []

    def _add(candidate: str) -> None:
        if candidate not in candidates:
            candidates.append(candidate)

    # Resolve sibling spec paths relative to the HTML *file* (not a fake
    # directory URL), so e.g. ``doc.json`` next to ``/swagger/index.html``
    # becomes ``/swagger/doc.json`` rather than ``/swagger/index.html/doc.json``.
    for name in SWAGGER_UI_SPECS:
        _add(urljoin(html_url, name))
    # Also try the page-as-directory form for setups that actually serve the
    # spec under the page path (rare but happens).
    dir_url = html_url if html_url.endswith("/") else html_url + "/"
    for name in SWAGGER_UI_SPECS:
        _add(urljoin(dir_url, name))
    # Plain root — covers "I put swagger.json at the site root" setups.
    _add(f"{parsed.scheme}://{parsed.netloc}/swagger.json")
    _add(f"{parsed.scheme}://{parsed.netloc}/openapi.json")
    _add(f"{parsed.scheme}://{parsed.netloc}/api-docs")
    _add(f"{parsed.scheme}://{parsed.netloc}/api-docs/swagger.json")
    _add(f"{parsed.scheme}://{parsed.netloc}/api-docs/openapi.json")
    # Parent-directory sibling — helps when the page is one level deeper than
    # the spec (e.g. ``/swagger/index.html`` + ``/doc.json``).
    parent = html_url.rsplit("/", 1)[0] + "/"
    for name in SWAGGER_UI_SPECS:
        _add(urljoin(parent, name))

    # Extract any ``url: "..."``/``configUrl: "..."`` literal in the HTML.
    # Prefer resources reachable without an extra host hop (relative paths).
    for raw in _extract_url_literals(html_url, html_url):
        if raw.startswith("/"):
            _add(f"{parsed.scheme}://{parsed.netloc}{raw.rstrip('/')}")
        elif raw.startswith(("http://", "https://")):
            _add(raw)
        else:
            _add(urljoin(dir_url, raw))

    return candidates


class _JsLiteralExtractor(HTMLParser):
    """
    Minimal HTML parser that collects double- and single-quoted string literals
    from ``<script>`` blocks.  Good enough for "url: 'doc.json'" patterns
    without pulling in a real JS parser.
    """

    def __init__(self) -> None:
        super().__init__()
        self._in_script = False
        self._buf: list[str] = []

    def handle_starttag(self, tag: str, attrs: Any) -> None:
        if tag == "script":
            self._in_script = True
            self._buf.clear()

    def handle_endtag(self, tag: str) -> None:
        if tag == "script" and self._in_script:
            self._in_script = False

    def handle_data(self, data: str) -> None:
        if self._in_script:
            self._buf.append(data)

    def literals(self) -> list[str]:
        text = "".join(self._buf)
        out: list[str] = []
        for m in _SWAGGER_UI_VAR_RE.finditer(text):
            val = m.group(1)
            if val and val not in out:
                out.append(val)
        return out


def _extract_url_literals(html_url: str, html: str) -> list[str]:
    try:
        p = _JsLiteralExtractor()
        p.feed(html)
        return p.literals()
    except Exception:
        logger.warning("Failed to scan HTML for JS spec-url literals for %s", html_url)
        return []


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def load_spec_from_text(text: str) -> dict[str, Any]:
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
                raise ValueError(
                    "Parsed YAML is not a dictionary (likely HTML or plain text)."
                )
            return res
        except Exception as e:
            raise ValueError(
                "Failed to parse spec as JSON or YAML. Ensure you are providing "
                "the raw JSON/YAML endpoint, not the Swagger UI HTML page."
            ) from e


def fetch_spec_from_url(url: str, timeout: float = 10) -> str:
    """
    Fetch an OpenAPI/Swagger spec from *url*.

    If *url* points at a Swagger UI HTML page (``text/html``), automatically
    resolve the underlying spec JSON/YAML using sibling-path heuristics and any
    inline JS configuration found in the page, so that inputs like
    ``https://host/swagger/index.html`` work without the caller needing to know
    whether the real spec is ``doc.json``, ``swagger.json``, ``/api-docs``, etc.
    """
    req = requests.Request("GET", url)
    resp = requests.Session().send(
        req.prepare(),
        timeout=timeout,
        allow_redirects=True,
    )
    resp.raise_for_status()
    ct = (resp.headers.get("Content-Type") or "").lower()
    body = resp.text

    if "text/html" in ct or body.lstrip().startswith("<!doctype") or body.lstrip().startswith("<html"):
        logger.info("URL %s returned HTML — attempting Swagger UI spec discovery", url)
        for candidate in _resolve_ui_candidate_urls(url):
            try:
                r2 = requests.Session().send(
                    requests.Request("GET", candidate, headers={"User-Agent": "QAI-Import/1.0", "Accept": "application/json,*/*"}).prepare(),
                    timeout=timeout,
                    allow_redirects=True,
                )
                if r2.status_code == 200:
                    stripped = r2.text.lstrip()
                    if stripped.startswith("{") or stripped.startswith("---"):
                        logger.info(
                            "Discovered spec at %s while fetching %s", candidate, url
                        )
                        return r2.text
            except Exception as exc:
                logger.debug("Swagger UI candidate %s failed: %s", candidate, exc)
        raise ValueError(
            f"Could not locate an OpenAPI/Swagger spec for the Swagger UI page at {url}. "
            "Try pointing directly at the JSON/YAML endpoint instead."
        )

    return body


def validate_openapi(spec_dict: dict[str, Any]) -> tuple[bool, str | None]:
    try:
        validate_spec(spec_dict)
        return True, None
    except Exception as e:
        # Catching everything because openapi_spec_validator exceptions
        # hierarchy is inconsistent across versions.
        return False, str(e)


def extract_base_url(spec: dict[str, Any], fallback: str | None = None) -> str | None:
    """
    Attempt to derive a base URL from servers (OpenAPI v3) or schemes/host/basePath (v2).

    Swagger 2.0 ``host`` values like ``localhost:8080`` are almost always
    copy-paste artifacts from local development.  When the spec came from a
    reachable Swagger UI page we already know the real host — that value is
    supplied as *fallback* by the caller (the resolved HTML URL's origin), so
    prefer it over the spec-internal ``host`` unless the spec itself declares an
    externally-reachable server.
    """
    if not spec:
        return fallback

    # OpenAPI v3
    servers = spec.get("servers")
    if servers and isinstance(servers, list):
        for srv in servers:
            if not isinstance(srv, dict):
                continue
            url = srv.get("url")
            if url:
                return url

    # Swagger v2
    host = spec.get("host")
    schemes = spec.get("schemes")
    base_path = spec.get("basePath", "")
    if host:
        def _looks_dev(h: str) -> bool:
            low = h.lower()
            return ("localhost" in low) or (low.startswith("127.0.0.1")) or (low.startswith("0.0.0.0"))

        scheme = schemes[0] if schemes else "https"
        computed = f"{scheme}://{host}{base_path}"
        if _looks_dev(host) and fallback:
            return fallback
        return computed

    return fallback


def parse_paths_to_endpoints(
    spec: dict[str, Any], project_obj=None, default_base_url=None
) -> dict[str, Any]:
    """
    Walk `paths` and convert into endpoint dicts ready to be saved.
    Returns tuple (imported_count, skipped_count, errors_list, endpoints_list)
    endpoints_list = [{method, path, summary, parameters, requestBody, responses, security}]
    """
    results: dict[str, Any] = {
        "imported": 0,
        "skipped": 0,
        "errors": [],
        "endpoints": [],
    }
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
            results["errors"].append(
                f"Unexpected operations structure for path {raw_path}"
            )
            continue

        for method, op_obj in operations.items():
            try:
                m = method.upper()
                # skip summary-only or vendor extensions
                if m not in {"GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"}:
                    results["skipped"] += 1
                    continue

                # Ensure base_url has trailing slash for urljoin to work correctly with paths
                effective_base = base_url or ""
                if effective_base and not effective_base.endswith("/"):
                    effective_base += "/"

                # Improved Schema Extraction with Ref Resolution
                request_body: dict[str, Any] = {}

                def resolve_schema(s: Any) -> dict[str, Any]:
                    if not isinstance(s, dict):
                        return {}
                    if "$ref" in s:
                        ref_path = s["$ref"].split("/")
                        ref_key = ref_path[-1]
                        # Look in definitions (Swagger 2) or components/schemas (OpenAPI 3)
                        definitions = (
                            spec.get("definitions", {})
                            or spec.get("components", {}).get("schemas", {})
                        )
                        return resolve_schema(definitions.get(ref_key, {}))
                    return (
                        s.get("properties")
                        or s.get("example")
                        or s
                    )

                rb_obj = op_obj.get("requestBody")
                if rb_obj and isinstance(rb_obj, dict):
                    content = rb_obj.get("content", {})
                    for _, media_type in content.items():
                        schema_obj = media_type.get("schema", {})
                        if schema_obj:
                            request_body = resolve_schema(schema_obj)
                            break

                # Fallback and Parameter Flattening (for Swagger 2 or Query Params)
                flattened_query: dict[str, str] = {}
                params = op_obj.get("parameters", [])
                for p in params:
                    if not isinstance(p, dict):
                        continue
                    p_name = p.get("name")
                    p_in = p.get("in")

                    if p_in == "body" and not request_body:
                        request_body = resolve_schema(p.get("schema", {}))
                    elif p_in == "formData":
                        request_body[p_name] = p.get("type", "string")
                    elif p_in == "query":
                        flattened_query[p_name] = p.get("type", "string")

                endpoint: dict[str, Any] = {
                    "method": m,
                    "path": raw_path,
                    "full_url": urljoin(effective_base, raw_path.lstrip("/")),
                    "name": op_obj.get("summary")
                    or op_obj.get("operationId")
                    or f"{m} {raw_path}",
                    "description": op_obj.get("description", ""),
                    "parameters": op_obj.get("parameters", []),
                    "requestBody": rb_obj,  # keep raw for full context
                    "flattened_body": request_body,  # Helper for the AI
                    "flattened_query": flattened_query,
                    "responses": op_obj.get("responses", {}),
                    "security": op_obj.get("security", spec.get("security", [])),
                }
                results["endpoints"].append(endpoint)
                results["imported"] += 1
            except Exception as e:
                logger.exception("Failed to parse operation %s %s", method, raw_path)
                results["errors"].append(
                    f"Failed to parse {method} {raw_path}: {str(e)}"
                )

    return results

"""
QA-standard reporting helpers.

These helpers power the structured, QA-grade sections of session / batch /
run reports: every failure gets a root-cause classification (with a plain-
language explanation and a suggested next step), and every test that flips
between pass and fail is flagged as flaky so engineers don't chase ghosts.

The taxonomy below intentionally mirrors the categories a human QA engineer
would bucket a failure into when triaging a test run.
"""
import re

from django.db.models import Q

# ---------------------------------------------------------------------------
# Failure taxonomy
# ---------------------------------------------------------------------------

FAILURE_TAXONOMY = {
    "http_status": {
        "label": "HTTP status mismatch",
        "summary": "The API returned an unexpected HTTP status code.",
        "suggestion": "Compare the expected vs actual status and confirm the app contract with the spec.",
    },
    "timeout": {
        "label": "Timeout / slow response",
        "summary": "The request did not complete within the allotted time.",
        "suggestion": "Retry once; if it persists, profile the endpoint for latency or check downstream services.",
    },
    "auth": {
        "label": "Authentication / authorization",
        "summary": "The request was rejected because of missing, invalid, or insufficient credentials.",
        "suggestion": "Verify the token/credential used for this test, its expiry, and the required role.",
    },
    "validation": {
        "label": "Input validation / schema error",
        "summary": "The payload or query parameters failed validation or schema checks.",
        "suggestion": "Align the request payload with the OpenAPI schema; 4xx here may be correct behaviour.",
    },
    "assertion": {
        "label": "Assertion failed",
        "summary": "The request succeeded but the response body did not match the asserted values.",
        "suggestion": "Inspect the assertion that failed and confirm whether the code or the test expectation is wrong.",
    },
    "server_error": {
        "label": "Server error (5xx)",
        "summary": "The application raised an internal error (5xx response).",
        "suggestion": "Pull the backend logs around the run timestamp; this is a product bug candidate.",
    },
    "network": {
        "label": "Network / connection error",
        "summary": "The runner could not reach the target (DNS, refused connection, reset, TLS).",
        "suggestion": "Check target availability, network egress, and TLS certificates from the runner.",
    },
    "script_error": {
        "label": "Test script error",
        "summary": "The test harness itself raised an error (bad script, locator, or runtime).",
        "suggestion": "Review the generated script / selector; this is an automation bug, not a product bug.",
    },
    "infra": {
        "label": "Infrastructure / runner issue",
        "summary": "The sandbox, browser, or runner infrastructure failed (crash, quota, session loss).",
        "suggestion": "Re-run on a fresh sandbox; if persistent, check runner health and quotas.",
    },
    "unknown": {
        "label": "Unclassified failure",
        "summary": "The failure does not match a known pattern yet.",
        "suggestion": "Review the raw error below and add a classification rule if a pattern emerges.",
    },
}

# Order matters: first match wins.
_RULES = [
    # Infrastructure & sandbox
    ("infra", re.compile(r"\b(sandbox|e2b|session (lost|expired)|browser (crash|closed)|runner|quota|no sandbox)\b", re.I)),
    # Script / harness errors
    ("script_error", re.compile(r"\b(syntaxerror|typeerror|referenceerror|attributeerror|selector|locator|timeout waiting for|element not found|no such element|playwright|selenium|script error|import error|traceback)\b", re.I)),
    # Timeouts (after script-level timeouts, generic timeouts apply to requests)
    ("timeout", re.compile(r"\b(timeout|timed out|etimedout|time limit exceeded|took too long)\b", re.I)),
    # Network
    ("network", re.compile(r"\b(connection (refused|reset|closed)|econnrefused|econnreset|dns|unreachable|ssl|tls|certificate|name or service not known|socket)\b", re.I)),
    # Server errors (5xx)
    ("server_error", re.compile(r"\b5\d{2}\b|\binternal server error\b|\bbad gateway\b|\bservice unavailable\b", re.I)),
    # Auth failures (4xx + auth keywords)
    ("auth", re.compile(r"\b(401|403|unauthori[sz]ed|forbidden|invalid token|expired token|login required|authentication|permission denied|credentials)\b", re.I)),
    # Validation (4xx + validation keywords)
    ("validation", re.compile(r"\b(400|422|validation|invalid (request|payload|body|schema|json)|missing (field|parameter|required)|required field|schema (error|mismatch))\b", re.I)),
    # Assertion mismatches
    ("assertion", re.compile(r"\b(assert|assertion|expected .* but (got|found)|did not match|mismatch)\b", re.I)),
]

# Keyword-free 4xx statuses default to validation unless auth-specific.
_STATUS_HINT = {
    400: "validation",
    401: "auth",
    403: "auth",
    404: "http_status",
    405: "http_status",
    409: "validation",
    413: "validation",
    415: "validation",
    422: "validation",
    429: "timeout",  # rate limited — retryable
    500: "server_error",
    502: "server_error",
    503: "server_error",
    504: "timeout",
}


def classify_failure(error_text=None, response_status=None, status_text=None):
    """
    Classify a failure into the QA taxonomy.

    Returns a dict with 'category', 'label', 'summary', 'suggestion'.
    Never raises — worst case returns the 'unknown' bucket.
    """
    text = " ".join(
        str(part) for part in (error_text, status_text)
        if part
    )
    if text:
        for category, pattern in _RULES:
            if pattern.search(text):
                return {"category": category, **FAILURE_TAXONOMY[category]}
    if response_status:
        category = _STATUS_HINT.get(int(response_status))
        if category:
            return {"category": category, **FAILURE_TAXONOMY[category]}
    return {"category": "unknown", **FAILURE_TAXONOMY["unknown"]}


def is_flaky_statuses(statuses):
    """A test whose runs contain both a pass and a fail is flaky."""
    statuses = {str(s).lower() for s in statuses if s}
    return bool(statuses & {"passed"}) and bool(statuses & {"failed", "error"})


def analyze_runs(runs):
    """
    Group a queryset/list of TestRuns by test case and produce a QA-grade
    per-test analysis:

        {
          "total_runs": int,
          "passed": int,
          "failed": int,
          "flaky": bool,
          "pass_rate": float,
          "avg_response_time_ms": float,
          "latest_status": str,
          "latest_run_id": int|None,
          "failure_breakdown": {category: count},
          "failures": [ {run_id, response_status, error_message,
                         classification: {...}} ... ],  # capped
        }

    Runs must expose: test_case_id, status, response_status,
    error_message, response_time_ms, id, executed_at.
    """
    from collections import defaultdict
    from django.db.models import Avg

    groups = defaultdict(list)
    for run in runs:
        groups[run.test_case_id].append(run)

    analyzed = []
    for tc_id, tc_runs in groups.items():
        ordered = sorted(tc_runs, key=lambda r: r.executed_at or r.id, reverse=True)
        statuses = [r.status for r in ordered]
        passed = sum(1 for s in statuses if s == "passed")
        failed = sum(1 for s in statuses if s in ("failed", "error"))
        total = len(ordered)
        avg_time = sum(r.response_time_ms or 0 for r in ordered) / total if total else 0

        failure_breakdown = defaultdict(int)
        failures = []
        for run in ordered:
            if run.status in ("failed", "error"):
                cls = classify_failure(run.error_message, run.response_status)
                failure_breakdown[cls["category"]] += 1
                if len(failures) < 5:
                    failures.append({
                        "run_id": run.id,
                        "response_status": run.response_status,
                        "error_message": (run.error_message or "")[:2000],
                        "classification": cls,
                    })

        test_case = None
        if tc_id and hasattr(tc_runs[0], "test_case"):
            test_case = tc_runs[0].test_case

        analyzed.append({
            "test_case_id": tc_id,
            "test_name": getattr(test_case, "name", None) or f"Test #{tc_id}",
            "endpoint": (
                f"{test_case.endpoint.method} {test_case.endpoint.url}"
                if test_case and test_case.endpoint else None
            ),
            "total_runs": total,
            "passed": passed,
            "failed": failed,
            "flaky": is_flaky_statuses(statuses),
            "pass_rate": round((passed / total) * 100, 1) if total else 0.0,
            "avg_response_time_ms": round(avg_time, 1),
            "latest_status": ordered[0].status if ordered else "pending",
            "latest_run_id": ordered[0].id if ordered else None,
            "failure_breakdown": dict(failure_breakdown),
            "failures": failures,
        })

    return analyzed


def flaky_summary(test_case_id, limit=10):
    """
    Look back at the most recent runs of a single test case and report whether
    the test has been flipping between pass and fail (the classic flake
    signature across CI re-runs).
    """
    from .models import TestRun

    runs = list(
        TestRun.objects.filter(test_case_id=test_case_id)
        .order_by("-executed_at")[:limit]
    )
    if not runs:
        return {"flaky": False, "checked_runs": 0, "passed": 0, "failed": 0}
    statuses = [r.status for r in runs]
    passed = sum(1 for s in statuses if s == "passed")
    failed = sum(1 for s in statuses if s in ("failed", "error"))
    return {
        "flaky": is_flaky_statuses(statuses),
        "checked_runs": len(runs),
        "passed": passed,
        "failed": failed,
    }


def failure_breakdown(runs):
    """Aggregate failure counts by root-cause category across a run set."""
    from collections import defaultdict

    counts = defaultdict(int)
    for run in runs:
        if run.status in ("failed", "error"):
            cls = classify_failure(run.error_message, run.response_status)
            counts[cls["category"]] += 1
    return dict(counts)


def qa_verdict(pass_rate, flaky_count=0):
    """
    Translate numbers into a QA verdict string used on report headers.
    """
    if pass_rate >= 90 and flaky_count == 0:
        return "PASS"
    if pass_rate >= 90:
        return "PASS (with flaky tests)"
    if pass_rate >= 60:
        return "CONDITIONAL"
    return "FAIL"

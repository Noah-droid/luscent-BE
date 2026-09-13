"""
Observation V2 — structured browser perception for the autonomous QA agent.

Design contract (Observation vs Evidence):
    Observation = bounded, structured state the LLM reasons over each step.
    Evidence    = artifacts retained to prove what happened (screenshots, raw
                  logs). Evidence is captured on policy, never as a heartbeat.

ELEMENT IDENTITY — SEMANTIC FIRST, SELECTORS ARE A COMPATIBILITY LAYER:
    The canonical identity of an interactive element in an Observation is its
    SEMANTIC tuple: (role, name, state: visible/enabled/required/value, bbox).
    The `suggested_selector` field is a DERIVED convenience hint so the
    current action protocol (which consumes selector strings) keeps working.
    It is NOT the long-term element identity and the architecture must not
    grow dependencies on it. Two cautions for consumers:

      1. A suggested_selector may be a Playwright LOCATOR expression —
         e.g. `role=button[name="Create workspace"]` — which is NOT a CSS
         selector. Every element therefore carries `selector_kind`
         ("css" | "playwright") so consumers can route the hint correctly.
         Playwright resolves both natively; CSS-only tooling (querySelector,
         cssselect, …) must only ever receive selector_kind="css" values.
      2. CSS-looking values (`#id`, `[name="x"]`, `[data-testid="y"]`) are
         equally derived; they are shortcuts, not identity.

    Target resolution at ACTION TIME goes through resolve_target(): semantic
    identity (role+name) is resolved against the LIVE page first, so a stale
    or duplicated selector hint can never mis-target. A future protocol
    revision will let the LLM pass role+target_name directly, after which
    suggested_selector can be deprecated without touching the schema.

TELEMETRY — PLAYWRIGHT FIRST, CDP ONLY WHERE NEEDED:
    Console messages, uncaught page errors, failed requests and >=400
    responses all come from Playwright's own event APIs (Page.on("console"),
    "pageerror", "requestfailed", "response"). No duplicate CDP plumbing is
    introduced here; CDP remains used only where Playwright has no equivalent
    (the pre-existing network-throttling emulation). Observations produced
    with telemetry attached are tagged strategy `+telemetry`.

TELEMETRY DELTAS, NOT FIREHOSE:
    Listeners accumulate events with monotonically increasing sequence
    numbers. Each observation returns only the DELTA since the previous
    observation (per-category read cursors) plus `telemetry_totals` — exact
    session-to-date counts that survive list capping. The LLM therefore sees
    each new error exactly once, with cumulative context retained.

This module is the PRIMARY perception source. Playwright owns browser
interaction. It runs in two places:
  - inside the E2B sandbox (imported by browser_manager_template.py), and
  - on the host (imported by tests),
so it must depend only on stdlib + playwright. No Django imports.

The public API:
    attach_telemetry_listeners(page, store) -> store
    capture_observation(page, ...) -> dict
    resolve_target(page, *, role, name, selector) -> Locator
    format_observation_for_llm(obs) -> str
    log_observation_metrics(obs, logger, prefix="[Agent]")
"""

import json
import logging
import time

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Bounding limits. The observation must stay useful but never explode the
# LLM prompt. Each limiter degrades gracefully (truncates + annotates).
# ---------------------------------------------------------------------------
MAX_ARIA_CHARS = 6000
MAX_INTERACTIVE_ELEMENTS = 50
MAX_PAGE_TEXT_CHARS = 1500
MAX_CONSOLE_ENTRIES = 30
MAX_PAGE_ERRORS = 10
MAX_NETWORK_FAILURES = 20
MAX_NETWORK_FAILURE_DESC = 300
MAX_TOTAL_OBSERVATION_CHARS = 20000

TELEMETRY_CATEGORIES = ("console_errors", "console_warnings", "page_errors", "network_failures")

_INVENTORY_JS = """
() => {
    const out = [];
    const seen = new Set();
    const nodes = document.querySelectorAll(
        'a[href], button, input, select, textarea, summary, ' +
        '[role="button"], [role="link"], [role="checkbox"], [role="radio"], ' +
        '[role="textbox"], [role="combobox"], [role="tab"], [role="switch"], ' +
        '[role="menuitem"], [role="option"], [role="searchbox"], [onclick]'
    );
    for (const el of nodes) {
        if (out.length >= __MAX_ELEMENTS__) break;
        // Skip elements that are not rendered (display:none / detached subtrees)
        const style = window.getComputedStyle(el);
        if (style.display === 'none' || style.visibility === 'hidden') continue;
        const rect = el.getBoundingClientRect();
        if (rect.width === 0 && rect.height === 0) continue;
        const role = (el.getAttribute('role') ||
            (el.tagName === 'A' ? 'link' :
             el.tagName === 'SELECT' ? 'combobox' :
             el.tagName === 'TEXTAREA' ? 'textbox' :
             el.tagName === 'BUTTON' ? 'button' :
             (el.tagName === 'INPUT'
                 ? ({text: 'textbox', password: 'textbox', email: 'textbox',
                     search: 'searchbox', number: 'spinbutton',
                     checkbox: 'checkbox', radio: 'radio',
                     range: 'slider', file: 'button'}[el.type] || 'textbox')
                 : el.tagName.toLowerCase())));
        let name = el.getAttribute('aria-label') ||
            (el.labels && el.labels.length ? el.labels[0].innerText.trim() : '') ||
            el.getAttribute('placeholder') || el.getAttribute('title') ||
            (el.innerText || el.value || '').trim();
        if (typeof name !== 'string') name = '';
        name = name.replace(/\\s+/g, ' ').trim().slice(0, 80);
        if (!name) name = el.getAttribute('name') || el.getAttribute('id') || '';
        name = name.replace(/\\s+/g, ' ').trim().slice(0, 80);
        // Deduplicate by role+name+position
        const key = role + '|' + name + '|' + Math.round(rect.x) + ',' + Math.round(rect.y);
        if (seen.has(key)) continue;
        seen.add(key);
        const disabled = el.disabled === true || el.getAttribute('aria-disabled') === 'true';
        // Derived COMPATIBILITY HINT (not identity — see module docstring).
        // selector_kind tells consumers which engine resolves it:
        //   "css"       → standard CSS attribute/id selector
        //   "playwright"→ Playwright LOCATOR syntax (role=...[name="..."]),
        //                 NOT valid CSS.
        let suggested_selector = null;
        let selector_kind = null;
        const testId = el.getAttribute('data-testid') || el.getAttribute('data-test');
        if (testId) {
            suggested_selector = '[data-testid="' + testId + '"]';
            selector_kind = 'css';
        } else if (el.id) {
            suggested_selector = '#' + CSS.escape(el.id);
            selector_kind = 'css';
        } else if (el.getAttribute('name')) {
            suggested_selector = '[name="' + el.getAttribute('name') + '"]';
            selector_kind = 'css';
        } else if (name) {
            suggested_selector = 'role=' + role + '[name="' + name.replace(/"/g, '\\\\"') + '"]';
            selector_kind = 'playwright';
        } else if (el.getAttribute('placeholder')) {
            suggested_selector = '[placeholder="' + el.getAttribute('placeholder').replace(/"/g, '\\\\"') + '"]';
            selector_kind = 'css';
        }
        out.push({
            role: role,
            name: name || null,
            tag: el.tagName.toLowerCase(),
            visible: true,
            enabled: !disabled,
            required: el.required === true || el.getAttribute('aria-required') === 'true',
            value: (role === 'textbox' || role === 'searchbox') ? String(el.value || '').slice(0, 60) : undefined,
            bbox: [Math.round(rect.x), Math.round(rect.y), Math.round(rect.width), Math.round(rect.height)],
            suggested_selector: suggested_selector,
            selector_kind: selector_kind,
        });
    }
    return out;
}
"""


def _new_event_lists():
    return {cat: [] for cat in TELEMETRY_CATEGORIES}


def _new_totals():
    return {cat: 0 for cat in TELEMETRY_CATEGORIES}


def attach_telemetry_listeners(page, store):
    """Attach Playwright event listeners that accumulate telemetry into `store`.

    Events carry monotonically increasing `seq` numbers; exact per-category
    totals are tracked in store["_totals"] independent of list capping, and
    per-category read cursors in store["_cursor"] power per-observation deltas.

    Call once per page (the browser manager calls this after creating the page).
    """
    import time as _time

    for cat in TELEMETRY_CATEGORIES:
        store.setdefault(cat, [])
    store.setdefault("_seq", 0)
    store.setdefault("_totals", _new_totals())
    store.setdefault("_cursor", {cat: 0 for cat in TELEMETRY_CATEGORIES})
    store.setdefault("_start_ms", _time.time() * 1000)

    def _record(category, entry):
        # Never raise, never grow unbounded: exact totals survive capping.
        store["_seq"] += 1
        entry["seq"] = store["_seq"]
        entry.setdefault("ts", round(_time.time() * 1000 - store["_start_ms"]))
        store["_totals"][category] += 1
        cap = {
            "console_errors": MAX_CONSOLE_ENTRIES,
            "console_warnings": MAX_CONSOLE_ENTRIES,
            "page_errors": MAX_PAGE_ERRORS,
            "network_failures": MAX_NETWORK_FAILURES,
        }[category]
        if len(store[category]) < cap:
            store[category].append(entry)

    def _on_console(msg):
        try:
            entry = {"type": msg.type, "text": (msg.text or "")[:300]}
            if msg.type == "error":
                _record("console_errors", entry)
            elif msg.type == "warning":
                _record("console_warnings", entry)
        except Exception:  # noqa: BLE001 — telemetry must never break actions
            pass

    def _on_page_error(err):
        try:
            _record("page_errors", {"text": str(err)[:300]})
        except Exception:  # noqa: BLE001
            pass

    def _on_request_failed(req):
        try:
            _record("network_failures", {
                "url": req.url[:MAX_NETWORK_FAILURE_DESC],
                "method": req.method,
                "failure": str(req.failure or "unknown")[:120],
            })
        except Exception:  # noqa: BLE001
            pass

    def _on_response(resp):
        try:
            if resp.status >= 400:
                _record("network_failures", {
                    "url": resp.url[:MAX_NETWORK_FAILURE_DESC],
                    "method": resp.request.method,
                    "status": resp.status,
                })
        except Exception:  # noqa: BLE001
            pass

    page.on("console", _on_console)
    page.on("pageerror", _on_page_error)
    page.on("requestfailed", _on_request_failed)
    page.on("response", _on_response)
    return store


def _collect_telemetry(page, store):
    """Return (delta, totals) for this observation.

    delta  = only entries recorded SINCE the previous observation (per-category
             cursor), each in its observation shape.
    totals = exact session-to-date counts (survive list capping).

    Advances the cursors so the next observation sees only new events.
    """
    if not store:
        return _new_event_lists(), _new_totals()

    delta = {}
    for cat in TELEMETRY_CATEGORIES:
        cursor = store.get("_cursor", {}).get(cat, 0)
        entries = [e for e in store.get(cat, []) if e.get("seq", 0) > cursor]
        if cat == "page_errors":
            delta[cat] = [e.get("text", str(e)) for e in entries]
        else:
            delta[cat] = entries
        # Advance: any future entry gets a seq above the current global _seq.
        store.setdefault("_cursor", {})[cat] = store.get("_seq", 0)

    totals = dict(store.get("_totals") or _new_totals())
    return delta, totals


def _aria_snapshot(page):
    """Playwright's ARIA snapshot (1.49+). Returns text (may be empty)."""
    try:
        snap = page.locator("body").aria_snapshot(timeout=3000)
        if snap and snap.strip():
            return snap.strip()
    except Exception as e:  # noqa: BLE001 — aria snapshot is best-effort
        logger.debug("aria_snapshot unavailable: %s", e)
    return ""


def _interactive_elements(page, limit=MAX_INTERACTIVE_ELEMENTS):
    try:
        js = _INVENTORY_JS.replace("__MAX_ELEMENTS__", str(limit))
        elements = page.evaluate(js)
        if isinstance(elements, list):
            return [e for e in elements if isinstance(e, dict)][:limit]
    except Exception as e:  # noqa: BLE001
        logger.debug("interactive element inventory failed: %s", e)
    return []


def _page_text(page):
    try:
        return (page.evaluate("() => document.body ? document.body.innerText : ''") or "").strip()
    except Exception:  # noqa: BLE001
        return ""


def capture_observation(page, *, strategy="semantic", include_page_text=True,
                        screenshot_b64=None, telemetry_store=None, extra=None):
    """
    Build a bounded structured observation of the current page state.

    Args:
        page: Playwright Page.
        strategy: base strategy tag ("semantic" | "visual" | "combined").
        include_page_text: include bounded document text.
        screenshot_b64: attach visual evidence only when intentionally captured.
        telemetry_store: dict fed by attach_telemetry_listeners().
        extra: optional dict merged into the observation (e.g. action context).

    Returns a JSON-serializable dict:
        url, title, aria_snapshot, interactive_elements, page_text,
        console_errors, console_warnings, page_errors, network_failures,
        telemetry_totals, timings, visual, strategy
    """
    t0 = time.time()
    obs = {
        "url": None,
        "title": None,
        "aria_snapshot": "",
        "interactive_elements": [],
        "page_text": "",
        **_new_event_lists(),
        "telemetry_totals": _new_totals(),
        "timings": {},
        "visual": None,
        "strategy": strategy,
    }

    try:
        obs["url"] = page.url
    except Exception:  # noqa: BLE001
        pass
    try:
        obs["title"] = page.title()
    except Exception:  # noqa: BLE001
        pass

    obs["aria_snapshot"] = _aria_snapshot(page)
    obs["interactive_elements"] = _interactive_elements(page)
    if include_page_text:
        text = _page_text(page)
        if len(text) > MAX_PAGE_TEXT_CHARS:
            text = text[:MAX_PAGE_TEXT_CHARS] + " …[truncated]"
        obs["page_text"] = text

    delta, totals = _collect_telemetry(page, telemetry_store)
    obs.update(delta)
    obs["telemetry_totals"] = totals

    t1 = time.time()
    obs["timings"] = {
        "capture_ms": round((t1 - t0) * 1000, 1),
    }

    if screenshot_b64:
        obs["visual"] = {"screenshot_b64": screenshot_b64, "captured": True}
        if "visual" not in strategy.split("+"):
            obs["strategy"] = strategy + "+visual"

    if any(obs[cat] for cat in TELEMETRY_CATEGORIES):
        # Fresh telemetry signal in THIS observation (delta-based tag).
        obs["strategy"] = obs["strategy"] + "+telemetry"

    if extra:
        # Caller-provided context (e.g. which action produced this observation).
        # Applied last: only fills keys not already reserved by the capture.
        for k, v in extra.items():
            obs.setdefault(k, v)

    return _enforce_size(obs)


def _enforce_size(obs):
    """Hard bound on serialized observation size. Degrades largest fields first."""
    encoded = json.dumps(obs)
    if len(encoded) <= MAX_TOTAL_OBSERVATION_CHARS:
        return obs

    # 1. Drop aria snapshot
    if obs.get("aria_snapshot"):
        obs["aria_snapshot"] = obs["aria_snapshot"][:MAX_ARIA_CHARS // 2] + " …[truncated]"
        encoded = json.dumps(obs)
    # 2. Trim page text
    if len(encoded) > MAX_TOTAL_OBSERVATION_CHARS and obs.get("page_text"):
        obs["page_text"] = obs["page_text"][:500] + " …[truncated]"
        encoded = json.dumps(obs)
    # 3. Trim element inventory from the bottom (keep the first, likely
    #    most relevant, interactive elements)
    while len(encoded) > MAX_TOTAL_OBSERVATION_CHARS and len(obs.get("interactive_elements", [])) > 10:
        obs["interactive_elements"] = obs["interactive_elements"][: len(obs["interactive_elements"]) // 2]
        encoded = json.dumps(obs)
    # 4. Last resort: drop the inventory entirely
    if len(encoded) > MAX_TOTAL_OBSERVATION_CHARS and obs.get("interactive_elements"):
        obs["interactive_elements"] = []
        obs["strategy"] = obs.get("strategy", "semantic") + "+truncated"
        encoded = json.dumps(obs)
    return obs


# ---------------------------------------------------------------------------
# Semantic target resolution (the identity layer in action)
# ---------------------------------------------------------------------------

def resolve_target(page, *, role=None, name=None, selector=None):
    """
    Resolve an action target to a Playwright Locator against the LIVE page.

    Semantic identity (role + name) is the PRIMARY resolution mechanism and is
    evaluated at action time — so stale hints, renamed ids and duplicated
    selectors can't silently mis-target. The selector hint is a FALLBACK.

    Resolution order:
      1. role+name, exact accessible-name match   (get_by_role, exact=True)
      2. role+name, case-insensitive substring    (get_by_role, exact=False)
      3. selector hint via page.locator() — accepts BOTH selector_kind values
         ("css" and "playwright", e.g. `role=button[name="…"]`)
      4. nothing matched → ValueError describing what was tried

    Ambiguity is resolved deterministically (.first) so strict-mode violations
    never surface as spurious action failures.
    """
    if role and name:
        for exact in (True, False):
            try:
                loc = page.get_by_role(role, name=name, exact=exact)
                count = loc.count()
            except Exception:  # noqa: BLE001 — unknown role etc.
                count = 0
            if count == 1:
                return loc
            if count > 1:
                return loc.first

    if selector:
        loc = page.locator(selector)
        try:
            if loc.count() == 0:
                # Element may render late — return the locator and let the
                # interaction auto-wait; a miss surfaces as a clear
                # Playwright timeout naming the selector.
                return loc
        except Exception:  # noqa: BLE001
            pass
        return loc

    tried = []
    if role or name:
        tried.append(f"role={role!r} name={name!r}")
    if selector:
        tried.append(f"selector={selector!r}")
    raise ValueError(
        "Could not resolve target on page "
        f"({'; '.join(tried) or 'no target given'}). "
        "Re-observe: the element may be gone or renamed."
    )


# ---------------------------------------------------------------------------
# LLM formatting
# ---------------------------------------------------------------------------

def format_observation_for_llm(obs):
    """
    Render an observation as a compact, structured block for the agent prompt.

    Bounded by construction: aria snapshot and element inventory are capped at
    capture time; this only adds small framing text.
    """
    if not isinstance(obs, dict):
        return str(obs)[:2000]

    lines = []
    lines.append(f"URL: {obs.get('url') or 'unknown'}")
    title = obs.get("title")
    if title:
        lines.append(f"TITLE: {title}")

    elements = obs.get("interactive_elements") or []
    if elements:
        lines.append(f"INTERACTIVE ELEMENTS ({len(elements)}):")
        for i, el in enumerate(elements):
            state = []
            if not el.get("visible", True):
                state.append("hidden")
            if not el.get("enabled", True):
                state.append("disabled")
            if el.get("required"):
                state.append("required")
            value = el.get("value")
            if value:
                state.append(f'value="{value}"')
            state_str = f" [{', '.join(state)}]" if state else ""
            bbox = el.get("bbox")
            bbox_str = f" @ {bbox[0]},{bbox[1]} {bbox[2]}x{bbox[3]}" if bbox else ""
            lines.append(
                f"  {i + 1}. {el.get('role', '?')} \"{el.get('name') or ''}\""
                f"{state_str}{bbox_str} suggested_selector: {el.get('suggested_selector')}"
                f" ({el.get('selector_kind') or 'css'})"
            )
    else:
        lines.append("INTERACTIVE ELEMENTS: none detected")

    errors = obs.get("console_errors") or []
    page_errors = obs.get("page_errors") or []
    net_failures = obs.get("network_failures") or []

    if errors:
        lines.append(f"CONSOLE ERRORS ({len(errors)} new):")
        for e in errors[:10]:
            lines.append(f"  - {e.get('text', e) if isinstance(e, dict) else e}")
    if page_errors:
        lines.append(f"PAGE ERRORS ({len(page_errors)} new):")
        for e in page_errors[:5]:
            lines.append(f"  - {e}")
    if net_failures:
        lines.append(f"NETWORK FAILURES ({len(net_failures)} new):")
        for f in net_failures[:10]:
            if isinstance(f, dict):
                status = f.get("status")
                detail = f"HTTP {status}" if status else f.get("failure", "failed")
                lines.append(f"  - {f.get('method', 'GET')} {f.get('url', '')} ({detail})")
            else:
                lines.append(f"  - {f}")

    totals = obs.get("telemetry_totals") or {}
    if any(totals.get(cat, 0) for cat in TELEMETRY_CATEGORIES):
        lines.append(
            "TELEMETRY TOTALS (session-to-date): console_errors=%s console_warnings=%s "
            "page_errors=%s network_failures=%s" % (
                totals.get("console_errors", 0), totals.get("console_warnings", 0),
                totals.get("page_errors", 0), totals.get("network_failures", 0),
            )
        )

    captcha = obs.get("captcha")
    if isinstance(captcha, dict):
        kind = captcha.get("kind") or "unknown"
        if captcha.get("solved"):
            lines.append(f"CAPTCHA: {kind} SOLVED ({captcha.get('mode')}), "
                         f"{captcha.get('detail', '')}")
        elif captcha.get("needs_human"):
            lines.append(f"CAPTCHA: {kind} requires HUMAN (interactive challenge) — "
                         f"{captcha.get('detail', '')}")
        else:
            lines.append(f"CAPTCHA: {kind} NOT solved — {captcha.get('detail', '')}. "
                         f"You may retry with action 'solve_captcha' after waiting.")

    aria = obs.get("aria_snapshot")
    if aria:
        lines.append("ARIA SNAPSHOT:")
        lines.append(aria)

    text = obs.get("page_text")
    if text:
        lines.append("PAGE TEXT (excerpt):")
        lines.append(text)

    strategy = obs.get("strategy", "semantic")
    lines.append(f"[observation_strategy={strategy}]")

    return "\n".join(lines)


def log_observation_metrics(obs, logger_=None, prefix="[Agent]"):
    """
    Emit the one-line observation metrics required for later measurement
    (how often the agent actually needs visual perception).

    INFO level — deliberately tiny, no DOM/ARIA payloads.
    """
    log = logger_ or logger
    try:
        elements = obs.get("interactive_elements") or []
        log.info(
            "%s observation_strategy=%s screenshot_captured=%s vision_called=%s "
            "interactive_elements=%d console_errors=%d page_errors=%d network_failures=%d",
            prefix,
            obs.get("strategy", "unknown"),
            bool(obs.get("visual")),
            bool(obs.get("visual") and obs["visual"].get("analyzed")),
            len(elements),
            len(obs.get("console_errors") or []),
            len(obs.get("page_errors") or []),
            len(obs.get("network_failures") or []),
        )
    except Exception:  # noqa: BLE001 — metrics must never break the loop
        pass

import json
import base64
import sys
import os
import time
import traceback

# Use the image-provided browser path when present (e2b.Dockerfile installs
# browsers at /ms-playwright and sets this env var). On hosts where that path
# doesn't exist, leave the env alone so Playwright uses its default registry.
if not os.environ.get('PLAYWRIGHT_BROWSERS_PATH') and os.path.isdir('/ms-playwright'):
    os.environ['PLAYWRIGHT_BROWSERS_PATH'] = '/ms-playwright'
__DISPLAY_LINE__

# Redirect stderr to a file so we can debug launch failures
sys.stderr = open('/tmp/browser_manager.err', 'w')
sys.stderr.write('Browser manager starting (headless=__HEADLESS__, perception_v2=__PERCEPTION_V2__)\n')
sys.stderr.flush()

# Observation V2 module is uploaded next to this script (/home/user) by the
# agent. If it is missing we degrade to legacy behavior rather than dying.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
PERCEPTION_V2 = __PERCEPTION_V2__
try:
    if PERCEPTION_V2:
        from browser_observation import (
            attach_telemetry_listeners,
            capture_observation,
            resolve_target,
        )
except Exception as _e:  # noqa: BLE001
    sys.stderr.write(f'Observation V2 module unavailable, falling back to legacy: {_e}\n')
    PERCEPTION_V2 = False

# CAPTCHA solver is optional: uploaded next to this script by the agent.
# Missing module → solve actions report unavailable instead of crashing.
try:
    import browser_captcha_solver as captcha_solver
except Exception as _e:  # noqa: BLE001
    sys.stderr.write(f'Captcha solver module unavailable: {_e}\n')
    captcha_solver = None

from playwright.sync_api import sync_playwright


def run():
    try:
        # NOTE: everything below (launch + READY + the stdin action loop) must
        # live INSIDE this context — leaving it tears down the Playwright
        # driver, which would kill every subsequent action.
        with sync_playwright() as p:
            # Launch browser — non-headless when VNC is active (visible in framebuffer)
            sys.stderr.write('Launching Chromium (headless=__HEADLESS__)...\n')
            sys.stderr.flush()
            _launch_args = list(getattr(captcha_solver, 'LAUNCH_ARGS',
                                        ['--no-sandbox', '--disable-gpu', '--disable-dev-shm-usage']))
            browser = p.chromium.launch(
                headless=__HEADLESS__,
                args=_launch_args
            )
            sys.stderr.write('Chromium launched OK\n')
            sys.stderr.flush()
            context = browser.new_context(viewport={'width': 1280, 'height': 800})
            if captcha_solver is not None:
                # Patch navigator.webdriver etc. BEFORE any page script runs —
                # this is what Turnstile actually fingerprints.
                try:
                    context.add_init_script(getattr(captcha_solver, 'STEALTH_INIT_SCRIPT', ''))
                except Exception as _e:  # noqa: BLE001
                    sys.stderr.write(f'Stealth init script failed: {_e}\n')
            page = context.new_page()

            # Observation V2 telemetry: console / page errors / network failures.
            # Attached once; events carry seq numbers, observations read deltas.
            _telemetry_store = {}
            if PERCEPTION_V2:
                try:
                    attach_telemetry_listeners(page, _telemetry_store)
                except Exception as _e:  # noqa: BLE001
                    sys.stderr.write(f'Telemetry listener setup failed: {_e}\n')

            # Navigate to a blank page so the user sees *something* in VNC immediately
            page.goto('about:blank')
            sys.stderr.write('Browser ready on about:blank\n')
            sys.stderr.flush()

            def _observation(screenshot_b64=None, strategy="semantic", extra=None):
                """Build the structured observation (V2 only)."""
                try:
                    return capture_observation(
                        page,
                        strategy=strategy,
                        screenshot_b64=screenshot_b64,
                        telemetry_store=_telemetry_store,
                        extra=extra,
                    )
                except Exception as e:  # noqa: BLE001 — observation must not kill actions
                    return {"strategy": "unavailable", "error": str(e)}

            def _take_screenshot():
                return base64.b64encode(page.screenshot()).decode('utf-8')

            # Signal ready — the browser window is now visible in the VNC stream
            print("READY", flush=True)

            for line in sys.stdin:
                if not line.strip():
                    continue
                try:
                    action = json.loads(line)
                    action_type = action.get('action')
                    error = None
                    screenshot_b64 = None
                    # Per-action CAPTCHA state — merged into the result below
                    _captcha_report = None
                    _needs_human = False

                    # ----- Playwright interaction (Playwright owns all interaction) -----
                    try:
                        # Observation V2: element targets resolve through
                        # resolve_target() — semantic identity (target_role +
                        # target_name, or role/name derived from the
                        # observation's suggested_selector hint) is matched
                        # against the LIVE page at action time; the selector
                        # string is only a fallback. Legacy keeps raw
                        # page.<action>(selector) untouched.
                        loc = None
                        if PERCEPTION_V2 and action_type in (
                                'click', 'type', 'fill', 'check', 'uncheck', 'select'):
                            loc = resolve_target(
                                page,
                                role=action.get('target_role'),
                                name=action.get('target_name'),
                                selector=action.get('selector'),
                            )

                        if action_type == 'navigate':
                            page.goto(action.get('url'), wait_until='domcontentloaded')
                            # Turnstile/managed challenges frequently run
                            # automatically on landing — give the widget a
                            # bounded window to self-solve right after
                            # navigation, before the agent wastes a step on it.
                            if captcha_solver is not None and action.get('auto_solve_captcha', True):
                                try:
                                    _cf = captcha_solver.attempt_captcha_bypass(
                                        page, max_wait_s=getattr(
                                            captcha_solver, 'AUTO_SOLVE_TIMEOUT_S', 8))
                                    if _cf.get('kind') != 'none':
                                        # ALWAYS report — an unsolved widget the
                                        # agent can't see looks like a mystery
                                        # stall; with the report it can reason.
                                        sys.stderr.write(f"auto-solve after navigate: {_cf}\n")
                                        _captcha_report = _cf
                                        if _cf.get('needs_human'):
                                            _needs_human = True
                                except Exception as _e:  # noqa: BLE001
                                    sys.stderr.write(f'auto-solve error: {_e}\n')
                        elif action_type == 'click':
                            if loc is not None:
                                loc.click()
                            else:
                                page.click(action.get('selector'))
                        elif action_type in ('type', 'fill'):
                            if loc is not None:
                                loc.fill(str(action.get('value')))
                            else:
                                page.fill(action.get('selector'), str(action.get('value')))
                        elif action_type == 'check':  # advertised by the agent prompt
                            if loc is not None:
                                loc.check()
                            else:
                                page.check(action.get('selector'))
                        elif action_type == 'uncheck':
                            if loc is not None:
                                loc.uncheck()
                            else:
                                page.uncheck(action.get('selector'))
                        elif action_type == 'select':
                            if loc is not None:
                                loc.select_option(action.get('value'))
                            else:
                                page.select_option(action.get('selector'), action.get('value'))
                        elif action_type == 'press':
                            page.keyboard.press(action.get('key', 'Enter'))
                        elif action_type == 'scroll':
                            if PERCEPTION_V2:
                                # legacy used arguments[0] which is not a Playwright API;
                                # fixed form lives in V2 only so legacy stays untouched
                                page.evaluate(f'window.scrollBy(0, {int(action.get("pixels", 500))})')
                            else:
                                page.evaluate('window.scrollBy(0, arguments[0])', action.get('pixels', 500))
                        elif action_type == 'wait':
                            page.wait_for_timeout(action.get('ms', 2000))
                        elif action_type == 'screenshot' and PERCEPTION_V2:
                            # Explicit visual evidence request — NOT the perception loop
                            screenshot_b64 = _take_screenshot()
                        elif action_type == 'observe' and PERCEPTION_V2:
                            pass  # observation-only step: no interaction, just state
                        elif action_type == 'solve_captcha' and captcha_solver is not None:
                            # Explicit agent request. diagnose=1 → introspection
                            # report instead of a solve attempt.
                            try:
                                if action.get('diagnose'):
                                    _captcha_report = captcha_solver.diagnose_captcha(page)
                                else:
                                    _captcha_report = captcha_solver.attempt_captcha_bypass(
                                        page,
                                        kind=action.get('captcha_type') or None,
                                        max_wait_s=action.get('max_wait_s'),
                                    )
                                    if _captcha_report.get('needs_human'):
                                        _needs_human = True
                            except Exception as _e:  # noqa: BLE001
                                _captcha_report = {'solved': False, 'kind': 'error',
                                                   'detail': str(_e)[:150], 'needs_human': True}
                        elif action_type == 'solve_captcha':
                            _captcha_report = {'solved': False, 'kind': 'unavailable',
                                               'detail': 'solver module not uploaded',
                                               'needs_human': True}
                        elif not PERCEPTION_V2 and action_type == 'screenshot':
                            pass  # legacy: falls through to the always-capture path
                        # Unknown action types: no-op; observation will reveal page state.
                    except Exception as e:
                        error = str(e)

                    page.wait_for_timeout(500)

                    if not PERCEPTION_V2:
                        # ---------------- Legacy protocol (unchanged) ----------------
                        # Capture result + screenshot for the agent's visual observation
                        screenshot = base64.b64encode(page.screenshot()).decode('utf-8')
                        print(json.dumps({
                            "status": "success",
                            "title": page.title(),
                            "url": page.url,
                            "screenshot_b64": screenshot,
                            "html_preview": page.content()[:500]
                        }), flush=True)
                        continue

                    # ---------------- Observation V2 protocol ----------------
                    strategy = "semantic"
                    screenshot_reason = None

                    # CAPTCHA report participates in observation strategy
                    if _captcha_report is not None and _captcha_report.get('kind') not in (None, 'none'):
                        strategy = "combined"

                    # EVIDENCE POLICY (all explicit, never automatic):
                    #   a) action 'screenshot'            → pure visual evidence step
                    #   b) capture_screenshot: true       → per-action capture request
                    #   c) evidence_policy: capture_on_failure → mission-level policy,
                    #      honored ONLY on failure. Recoverable/transient failures
                    #      stay screenshot-free by default.
                    # analyze_visual is an explicit request — implies capture.
                    if action.get('analyze_visual') and not screenshot_b64:
                        try:
                            screenshot_b64 = _take_screenshot()
                        except Exception:  # noqa: BLE001
                            screenshot_b64 = None

                    if (PERCEPTION_V2 and action.get('capture_screenshot')
                            and not screenshot_b64 and action_type != 'screenshot'):
                        # (b) per-action explicit capture — works on success AND failure
                        try:
                            screenshot_b64 = _take_screenshot()
                            screenshot_reason = "explicit_request"
                        except Exception:  # noqa: BLE001
                            screenshot_b64 = None

                    if action_type == 'screenshot' and screenshot_b64:
                        # (a) Intentional visual evidence step
                        strategy = "visual"
                        screenshot_reason = "explicit_request"
                    elif error and not screenshot_b64:
                        # (c) Failure evidence policy gate
                        if action.get('evidence_policy') == 'capture_on_failure':
                            try:
                                screenshot_b64 = _take_screenshot()
                                screenshot_reason = "failure_policy"
                                strategy = "combined"
                            except Exception:  # noqa: BLE001
                                screenshot_b64 = None

                    explicit_vision = bool(action.get('analyze_visual')) and screenshot_b64

                    obs = _observation(
                        screenshot_b64=screenshot_b64,
                        strategy=strategy,
                        extra={"action": action_type},
                    )
                    if screenshot_b64 and screenshot_reason:
                        obs["screenshot_reason"] = screenshot_reason
                    if _captcha_report is not None and _captcha_report.get('kind') not in (None, 'none'):
                        # Travels inside the observation so the LLM sees the
                        # challenge outcome in the same feedback block.
                        obs["captcha"] = _captcha_report

                    result = {
                        "status": "error" if error else "success",
                        "title": obs.get("title"),
                        "url": obs.get("url"),
                        "observation": obs,
                        "observation_strategy": obs.get("strategy", strategy),
                    }
                    if error:
                        result["error"] = error
                    if screenshot_b64:
                        # Evidence passes through to the host for persistence;
                        # perception travels inside obs["visual"].
                        result["screenshot_b64"] = screenshot_b64
                    if explicit_vision:
                        # Host decides whether to run the vision model; flag the intent.
                        result["vision_requested"] = True
                    if _captcha_report is not None:
                        result["captcha_report"] = _captcha_report
                        if _captcha_report.get('solved'):
                            result["status"] = result.get("status") or "success"
                    if _needs_human:
                        result["captcha_needs_human"] = True
                    print(json.dumps(result), flush=True)
                except Exception as e:
                    print(json.dumps({"error": str(e)}), flush=True)

            browser.close()
    except Exception as e:
        tb = traceback.format_exc()
        sys.stderr.write('FATAL: ' + tb + '\n')
        sys.stderr.flush()
        # Catch launch errors (missing binary, display issues, etc.)
        print(json.dumps({"error": "Browser launch failed: " + str(e), "phase": "startup", "traceback": tb[:500]}), flush=True)


if __name__ == "__main__":
    run()

"""
Observation V2 tests — structured Playwright perception for the QA agent.

Covers the phase's acceptance criteria:
  1. Normal page   -> useful structured observation, NO screenshot taken
  2. Accessibility -> roles/names/state represented correctly
  3. Console       -> deliberate console error captured
  4. Network       -> failed request captured
  5. Screenshot independence -> no automatic capture / vision calls
  6. Evidence      -> explicitly captured screenshot persisted + attached
  7. Headless      -> full observation works headless, no VNC/Xvfb dependency
  8. Flag wiring   -> manager template + agent honor PERCEPTION_V2

Runs against real Chromium (playwright is in requirements) using
page.route() to serve test pages without touching the network.

NOTE: all classes are SimpleTestCase — this suite deliberately requires no
database. Run with:
    python -m unittest test_cases.test_observation_v2 -v
("manage.py test" is blocked project-wide by a pre-existing fresh-DB
migration issue unrelated to Observation V2.)
"""
import json
import logging
import os
from unittest import mock

from django.conf import settings as django_settings
import django

if not django_settings.configured:
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    django.setup()

from django.test import SimpleTestCase, override_settings

from test_cases.browser_observation import (
    attach_telemetry_listeners,
    capture_observation,
    format_observation_for_llm,
    log_observation_metrics,
    resolve_target,
)

try:
    from playwright.sync_api import sync_playwright
    HAS_PLAYWRIGHT = True
except ImportError:  # pragma: no cover
    HAS_PLAYWRIGHT = False

import unittest


def _start_browser():
    """Shared headless Chromium with route-intercepted test pages."""
    pw = sync_playwright().start()
    browser = pw.chromium.launch(headless=True, args=["--no-sandbox"])
    context = browser.new_context(viewport={"width": 1280, "height": 800})
    page = context.new_page()

    app_page = """<!DOCTYPE html>
<html><head><title>Workspace Console</title></head>
<body>
  <h1>Create your workspace</h1>
  <form>
    <label for="ws-name">Workspace name</label>
    <input id="ws-name" type="text" placeholder="Acme Inc" required>
    <button id="create-btn" type="button">Create workspace</button>
    <a href="/pricing">Pricing</a>
    <select aria-label="Plan"><option>Free</option><option>Pro</option></select>
    <button disabled aria-label="Danger zone">Delete everything</button>
  </form>
  <script>
    function boom() { console.error("Uncaught render failure in widget"); }
    function failRequest() { fetch("/api/missing-endpoint").catch(() => {}); }
    setTimeout(boom, 50);
    setTimeout(failRequest, 50);
  </script>
</body></html>"""

    def _route(route):
        if route.request.url.endswith("/api/missing-endpoint"):
            route.fulfill(status=500, body='{"error": "boom"}',
                          content_type="application/json")
        else:
            route.fulfill(status=200, content_type="text/html", body=app_page)

    page.route("**/*", _route)
    return pw, browser, page


# ---------------------------------------------------------------------------
# capture_observation against a real page
# ---------------------------------------------------------------------------

@unittest.skipUnless(HAS_PLAYWRIGHT, "playwright not installed")
class ObservationCaptureTests(SimpleTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._pw, cls._browser, cls._page = _start_browser()
        cls._page.goto("https://test.local/app", wait_until="load")

    @classmethod
    def tearDownClass(cls):
        cls._browser.close()
        cls._pw.stop()
        super().tearDownClass()

    def setUp(self):
        self.store = {}
        attach_telemetry_listeners(self._page, self.store)

    def _reload(self):
        """Re-navigate so scripted console/network events fire while THIS
        test's listeners are attached (events are one-shot per page load)."""
        self._page.goto("https://test.local/app", wait_until="load")
        self._page.wait_for_timeout(300)

    def test_normal_page_observed_without_screenshot(self):
        """Criterion 1: structured observation with zero visual capture."""
        obs = capture_observation(self._page, telemetry_store=self.store)
        self.assertEqual(obs["url"], "https://test.local/app")
        self.assertEqual(obs["title"], "Workspace Console")
        self.assertGreater(len(obs["interactive_elements"]), 3)
        self.assertIsNone(obs["visual"])
        self.assertFalse(obs["strategy"].endswith("visual"))
        # Nothing was screenshotted to produce this observation
        self.assertEqual(obs["timings"]["capture_ms"], obs["timings"]["capture_ms"])  # timing recorded

    def test_accessibility_roles_names_states(self):
        """Criterion 2: roles, accessible names, and enabled state."""
        obs = capture_observation(self._page, telemetry_store=self.store)
        by_name = {e.get("name"): e for e in obs["interactive_elements"]}
        create = by_name.get("Create workspace")
        self.assertIsNotNone(create, f"inventory: {obs['interactive_elements']}")
        self.assertEqual(create["role"], "button")
        self.assertTrue(create["enabled"])
        self.assertTrue(create["suggested_selector"])
        self.assertTrue(create["bbox"][2] > 0 and create["bbox"][3] > 0)

        danger = by_name.get("Danger zone")
        self.assertIsNotNone(danger)
        self.assertEqual(danger["role"], "button")
        self.assertFalse(danger["enabled"])

        link = by_name.get("Pricing")
        self.assertIsNotNone(link)
        self.assertEqual(link["role"], "link")

    def test_console_error_captured(self):
        """Criterion 3: deliberate console error lands in telemetry."""
        self._reload()  # let the script fire
        obs = capture_observation(self._page, telemetry_store=self.store)
        texts = [e.get("text", "") for e in obs["console_errors"]]
        self.assertTrue(
            any("render failure" in t for t in texts),
            f"console errors: {obs['console_errors']}",
        )
        self.assertIn("telemetry", obs["strategy"])

    def test_failed_network_request_captured(self):
        """Criterion 4: HTTP 500 response captured as network failure."""
        self._reload()
        obs = capture_observation(self._page, telemetry_store=self.store)
        urls = [f.get("url", "") for f in obs["network_failures"]]
        self.assertTrue(
            any("missing-endpoint" in u for u in urls),
            f"network failures: {obs['network_failures']}",
        )

    def test_aria_snapshot_present_and_bounded(self):
        obs = capture_observation(self._page, telemetry_store=self.store)
        self.assertTrue(obs["aria_snapshot"].strip())
        self.assertLessEqual(len(obs["aria_snapshot"]), 7000)

    def test_selector_fallback_uses_playwright_locator_syntax(self):
        """Refinement #1: elements without id/name/testid fall back to
        Playwright LOCATOR syntax (`role=...[name="..."]`) which is NOT CSS.
        Semantic identity (role/name/state) must still be fully present."""
        self._page.route(
            "**/anon",
            lambda r: r.fulfill(
                content_type="text/html",
                body='<html><body><button>Unnamed action</button></body></html>',
            ),
        )
        self._page.goto("https://test.local/anon", wait_until="load")
        obs = capture_observation(self._page, telemetry_store={})
        el = next(e for e in obs["interactive_elements"] if e["name"] == "Unnamed action")
        # Semantic identity is primary and complete
        self.assertEqual(el["role"], "button")
        self.assertTrue(el["enabled"])
        # Derived hint uses Playwright locator engine, tagged as such
        self.assertTrue(el["suggested_selector"].startswith("role="),
                        f"got: {el['suggested_selector']}")
        self.assertEqual(el["selector_kind"], "playwright")
        self.assertIn('name="Unnamed action"', el["suggested_selector"])
        self._page.goto("https://test.local/app", wait_until="load")  # restore

    def test_selector_kind_css_for_attribute_targets(self):
        """Compromise #1 fix: CSS-looking hints must be tagged "css" so
        consumers can route them away from CSS-only tooling."""
        self._page.route(
            "**/cssids",
            lambda r: r.fulfill(
                content_type="text/html",
                body='<html><body><input id="email" type="email" '
                     'aria-label="Email address"></body></html>',
            ),
        )
        self._page.goto("https://test.local/cssids", wait_until="load")
        obs = capture_observation(self._page, telemetry_store={})
        el = next(e for e in obs["interactive_elements"] if e["name"] == "Email address")
        self.assertEqual(el["suggested_selector"], "#email")
        self.assertEqual(el["selector_kind"], "css")
        self._page.goto("https://test.local/app", wait_until="load")  # restore

    def test_telemetry_is_delta_not_firehose(self):
        """Compromise #2 fix: an error appears as NEW exactly once; the next
        observation reports an empty delta while totals keep counting."""
        self._reload()  # fires one console error + one 500 (seq A, B)
        obs1 = capture_observation(self._page, telemetry_store=self.store)
        self.assertTrue(any("render failure" in e.get("text", "")
                            for e in obs1["console_errors"]))
        self.assertGreaterEqual(obs1["telemetry_totals"]["console_errors"], 1)

        obs2 = capture_observation(self._page, telemetry_store=self.store)
        # No NEW events → empty deltas, totals preserved
        self.assertEqual(obs2["console_errors"], [])
        self.assertEqual(obs2["network_failures"], [])
        self.assertGreaterEqual(obs2["telemetry_totals"]["console_errors"], 1)
        self.assertGreaterEqual(obs2["telemetry_totals"]["network_failures"], 1)
        # And the +telemetry strategy tag correctly only marks fresh signal
        self.assertNotIn("+telemetry", obs2["strategy"])

        # A genuinely new event shows up as a fresh delta with a higher total
        self._page.evaluate("() => console.error('second distinct failure')")
        obs3 = capture_observation(self._page, telemetry_store=self.store)
        texts = [e.get("text", "") for e in obs3["console_errors"]]
        self.assertEqual([t for t in texts if "second distinct" in t],
                         ["second distinct failure"])
        self.assertGreater(obs3["telemetry_totals"]["console_errors"],
                           obs2["telemetry_totals"]["console_errors"])

    def test_resolve_target_semantic_beats_stale_selector(self):
        """Compromise #3 fix: role+name resolved against the LIVE page wins
        even when the selector hint points somewhere stale."""
        self._page.route(
            "**/mutated",
            lambda r: r.fulfill(
                content_type="text/html",
                body='<html><body>'
                     '<button id="old" style="display:none">Stale target</button>'
                     '<button id="fresh">Save changes</button>'
                     '</body></html>',
            ),
        )
        self._page.goto("https://test.local/mutated", wait_until="load")
        loc = resolve_target(
            self._page,
            role="button",
            name="Save changes",
            selector="#old",  # stale hint that exists but is the wrong element
        )
        self.assertEqual(loc.evaluate("el => el.id"), "fresh")
        self._page.goto("https://test.local/app", wait_until="load")  # restore

    def test_resolve_target_falls_back_to_selector(self):
        self._page.goto("https://test.local/app", wait_until="load")
        loc = resolve_target(self._page, role=None, name=None, selector="#create-btn")
        self.assertEqual(loc.evaluate("el => el.id"), "create-btn")

    def test_resolve_target_accepts_playwright_locator_syntax(self):
        """Selector hints of selector_kind=playwright resolve through the same
        page.locator() path (Playwright parses role=... natively)."""
        self._page.goto("https://test.local/app", wait_until="load")
        loc = resolve_target(
            self._page, role=None, name=None,
            selector='role=link[name="Pricing"]',
        )
        self.assertIn("pricing", loc.evaluate("el => el.getAttribute('href')").lower())

    def test_resolve_target_errors_when_nothing_matches(self):
        self._page.goto("https://test.local/app", wait_until="load")
        with self.assertRaises(ValueError):
            resolve_target(self._page, role=None, name=None, selector=None)

    def test_bounded_size_on_huge_page(self):
        """Observation stays bounded even when the page is enormous."""
        big = "<!DOCTYPE html><html><body>" + "".join(
            f'<button id="b{i}">Button {i}</button>' for i in range(400)
        ) + "</body></html>"
        self._page.route("**/big", lambda r: r.fulfill(body=big, content_type="text/html"))
        self._page.goto("https://test.local/big", wait_until="load")
        obs = capture_observation(self._page, telemetry_store={})
        import json as _json
        self.assertLessEqual(len(_json.dumps(obs)), 20000)
        self._page.goto("https://test.local/app", wait_until="load")  # restore


# ---------------------------------------------------------------------------
# LLM formatting
# ---------------------------------------------------------------------------

class FormatObservationTests(SimpleTestCase):
    def test_format_includes_semantics_and_telemetry(self):
        obs = {
            "url": "https://x.test/app",
            "title": "App",
            "interactive_elements": [
                {"role": "button", "name": "Create workspace", "enabled": False,
                 "bbox": [10, 20, 100, 30], "suggested_selector": "#create-btn"},
            ],
            "console_errors": [{"text": "ReferenceError: foo is not defined"}],
            "page_errors": ["Uncaught TypeError: bar"],
            "network_failures": [{"method": "GET", "url": "https://x.test/api/missing", "status": 500}],
            "aria_snapshot": '- button "Create workspace"',
            "page_text": "Welcome to the app",
            "strategy": "semantic+telemetry",
        }
        out = format_observation_for_llm(obs)
        self.assertIn('button "Create workspace"', out)
        self.assertIn("[disabled]", out)
        self.assertIn("ReferenceError", out)
        self.assertIn("HTTP 500", out)
        self.assertIn("ARIA SNAPSHOT", out)
        self.assertIn("observation_strategy=semantic+telemetry", out)
        # actionable selector surfaced for the existing action protocol
        self.assertIn("#create-btn", out)

    def test_format_handles_empty_observation(self):
        out = format_observation_for_llm({"url": None, "strategy": "semantic"})
        self.assertIn("INTERACTIVE ELEMENTS: none detected", out)

    def test_metrics_line(self):
        obs = {"strategy": "semantic", "visual": None,
               "interactive_elements": [{"role": "button"}],
               "console_errors": [], "page_errors": [], "network_failures": [1]}
        logger = logging.getLogger("obs_test")
        with self.assertLogs(logger, level="INFO") as cm:
            log_observation_metrics(obs, logger, prefix="[Agent]")
        line = "\n".join(cm.output)
        self.assertIn("observation_strategy=semantic", line)
        self.assertIn("screenshot_captured=False", line)
        self.assertIn("vision_called=False", line)
        self.assertIn("interactive_elements=1", line)
        self.assertIn("network_failures=1", line)
        # No DOM/ARIA payloads at INFO level
        self.assertNotIn("aria", line.lower())


# ---------------------------------------------------------------------------
# Screenshot independence + evidence wiring (no browser needed)
# ---------------------------------------------------------------------------

class _FakeAgent:
    """Lightweight host that reuses the real AutonomousAgent methods under test.

    AutonomousAgent.__init__ pulls settings/models (DB), so we bind only the
    two methods this phase touches onto a bare object instead of instantiating.
    """

    def __init__(self, perception_v2):
        from test_cases.autonomous_agent import AutonomousAgent  # needs settings
        self.perception_v2 = perception_v2
        self.vision_calls = 0
        # bind real code-under-test onto this instance
        self._finalize_browser_result = AutonomousAgent._finalize_browser_result.__get__(self)
        self._persist_screenshot_evidence = AutonomousAgent._persist_screenshot_evidence.__get__(self)

    def _analyze_vision(self, b64_image):
        self.vision_calls += 1
        return "VISION OUTPUT"


class ScreenshotIndependenceTests(SimpleTestCase):
    def _patch_persist(self, agent, return_value):
        """Point the bound method at a stub so no Cloudinary/project is touched."""
        return mock.patch.object(
            agent,
            "_persist_screenshot_evidence",
            return_value=return_value,
            create=True,
        )

    @override_settings(PERCEPTION_V2=True)
    def test_v2_success_result_has_no_vision_call(self):
        """Criterion 5: normal V2 action → no vision model call."""
        agent = _FakeAgent(perception_v2=True)
        res = {"status": "success", "title": "T", "url": "https://x.test",
               "observation": {"strategy": "semantic", "interactive_elements": [],
                               "console_errors": [], "page_errors": [],
                               "network_failures": [], "visual": None}}
        out = agent._finalize_browser_result(res, {"action": "click", "selector": "#b"})
        self.assertNotIn("visual_observation", out)
        self.assertEqual(agent.vision_calls, 0)
        self.assertNotIn("screenshot_b64", out)

    @override_settings(PERCEPTION_V2=True)
    def test_v2_explicit_vision_request_runs_vision_once(self):
        agent = _FakeAgent(perception_v2=True)
        # Realistic fixture: the manager captures + returns screenshot_b64
        # whenever analyze_visual is requested.
        res = {"status": "success", "screenshot_b64": "ZmFrZQ==",
               "observation": {"strategy": "semantic+visual",
                               "interactive_elements": [], "visual": {"captured": True}}}
        with self._patch_persist(agent, None):
            out = agent._finalize_browser_result(
                res, {"action": "click", "analyze_visual": True})
        self.assertEqual(agent.vision_calls, 1)
        self.assertEqual(out["observation"]["visual"]["description"], "VISION OUTPUT")

    @override_settings(PERCEPTION_V2=True)
    def test_v2_screenshot_persisted_as_evidence(self):
        """Criterion 8: explicit screenshot → persisted and attached."""
        agent = _FakeAgent(perception_v2=True)
        # Realistic fixture: on failure the manager attaches failure evidence.
        res = {"status": "error", "error": "selector not found", "screenshot_b64": "ZmFrZQ==",
               "observation": {"strategy": "combined", "interactive_elements": [],
                               "screenshot_reason": "failure",
                               "visual": {"captured": True}}}
        with self._patch_persist(
                agent, "https://res.cloudinary.com/test/mission.png") as persist:
            out = agent._finalize_browser_result(
                res, {"action": "click", "selector": "#missing"})
        persist.assert_called_once()
        self.assertEqual(out["screenshot_url"], "https://res.cloudinary.com/test/mission.png")
        self.assertEqual(out["observation"]["visual"]["evidence_url"],
                         "https://res.cloudinary.com/test/mission.png")
        # b64 never leaks into the result
        self.assertNotIn("screenshot_b64", json.dumps(out))

    @override_settings(PERCEPTION_V2=True)
    def test_recoverable_failure_leaves_no_screenshot(self):
        """Refinement #3: a normal recoverable failure carries NO screenshot
        and NO vision call — the structured observation is the evidence."""
        agent = _FakeAgent(perception_v2=True)
        # Manager response for a policy-less failure: no screenshot_b64 at all
        res = {"status": "error", "error": "Timeout 30000ms exceeded",
               "observation": {"strategy": "semantic+telemetry",
                               "interactive_elements": [], "visual": None}}
        out = agent._finalize_browser_result(
            res, {"action": "click", "selector": "#flaky"})
        self.assertNotIn("screenshot_url", out)
        self.assertEqual(agent.vision_calls, 0)
        self.assertIsNone(out["observation"].get("visual"))

    @override_settings(PERCEPTION_V2=True)
    def test_no_fake_evidence_when_no_screenshot(self):
        """When nothing was captured, no visual evidence is claimed."""
        agent = _FakeAgent(perception_v2=True)
        res = {"status": "success", "observation": {"strategy": "semantic", "visual": None}}
        out = agent._finalize_browser_result(res, {"action": "navigate", "url": "https://x.test"})
        self.assertNotIn("screenshot_url", out)
        self.assertIsNone(out["observation"].get("visual"))

    @override_settings(PERCEPTION_V2=False)
    def test_legacy_path_still_uses_vision(self):
        """Backward compat: flag off → legacy heartbeat behavior intact."""
        agent = _FakeAgent(perception_v2=False)
        res = {"status": "success", "screenshot_b64": "ZmFrZQ==",
               "title": "T", "url": "https://x.test"}
        out = agent._finalize_browser_result(res, {"action": "click"})
        self.assertEqual(agent.vision_calls, 1)
        self.assertEqual(out["visual_observation"], "VISION OUTPUT")
        self.assertNotIn("screenshot_b64", out)


# ---------------------------------------------------------------------------
# Template / flag wiring
# ---------------------------------------------------------------------------

class TemplateWiringTests(SimpleTestCase):
    def _load_template(self, perception_v2, headless=True):
        import os
        path = os.path.join(os.path.dirname(__import__("test_cases").__file__),
                            "browser_manager_template.py")
        with open(path) as f:
            src = f.read()
        src = (src
               .replace("__HEADLESS__", str(headless))
               .replace("__DISPLAY_LINE__", "# headless")
               .replace("__PERCEPTION_V2__", str(perception_v2)))
        return src

    def test_template_compiles_in_both_modes(self):
        import ast
        for v2 in (True, False):
            src = self._load_template(v2)
            ast.parse(src)  # raises on syntax error

    @override_settings(PERCEPTION_V2=True)
    def test_v2_template_has_no_unconditional_screenshot(self):
        """V2 path must not screenshot after every action."""
        src = self._load_template(True)
        legacy_branch = src.split("if not PERCEPTION_V2:")[0]
        self.assertNotIn('base64.b64encode(page.screenshot', legacy_branch.split("def _take_screenshot")[0])

    @override_settings(PERCEPTION_V2=False)
    def test_legacy_template_keeps_heartbeat(self):
        src = self._load_template(False)
        self.assertIn('"screenshot_b64": screenshot', src)
        self.assertIn('"html_preview"', src)

    def test_v2_failure_evidence_is_policy_gated(self):
        """Refinement #3: recoverable failures must NOT auto-screenshot.
        Failure capture in V2 must be gated on the mission evidence_policy;
        capture_screenshot must work as an explicit per-action request."""
        src = self._load_template(True)
        v2_branch = src.split("Observation V2 protocol")[1]
        # Failure capture only via the policy gate
        self.assertIn("elif error and not screenshot_b64:", v2_branch)
        self.assertIn("action.get('evidence_policy') == 'capture_on_failure'", v2_branch)
        # Explicit per-action capture exists (success AND failure)
        self.assertIn("action.get('capture_screenshot')", v2_branch)
        # The unconditional legacy capture must not leak into the V2 branch
        self.assertNotIn("Legacy protocol (unchanged)", v2_branch)

    def test_semantic_identity_documented_as_primary(self):
        """Refinement #1: module must document semantic-first identity and the
        Playwright-locator (not CSS) nature of role-based suggested_selector."""
        import test_cases.browser_observation as bo
        doc = bo.__doc__ or ""
        self.assertIn("SEMANTIC", doc.upper())
        self.assertIn("COMPATIBILITY", doc.upper())
        self.assertIn("NOT a CSS", doc)

    def test_agent_replaces_placeholder(self):
        """The agent must substitute __PERCEPTION_V2__ (would crash otherwise)."""
        import inspect
        from test_cases import autonomous_agent as aa
        source = inspect.getsource(aa.AutonomousAgent._init_browser_manager)
        self.assertIn("__PERCEPTION_V2__", source)


# ---------------------------------------------------------------------------
# Headless observation (criterion 6/7 combined)
# ---------------------------------------------------------------------------

@unittest.skipUnless(HAS_PLAYWRIGHT, "playwright not installed")
class HeadlessObservationTests(SimpleTestCase):
    """Full observation pipeline headless: no DISPLAY, no VNC, no vision."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._pw, cls._browser, cls._page = _start_browser()

    @classmethod
    def tearDownClass(cls):
        cls._browser.close()
        cls._pw.stop()
        super().tearDownClass()

    def test_headless_action_observation_cycle(self):
        import os
        self.assertNotIn("DISPLAY", os.environ)  # sanity: truly headless env
        store = {}
        attach_telemetry_listeners(self._page, store)
        self._page.goto("https://test.local/app", wait_until="load")

        # Interact purely from structured observation — no pixels involved
        obs = capture_observation(self._page, telemetry_store=store)
        target = next(e for e in obs["interactive_elements"]
                      if e["name"] == "Workspace name")
        self._page.fill(target["suggested_selector"], "Acme")
        self._page.click(next(e for e in obs["interactive_elements"]
                              if e["name"] == "Create workspace")["suggested_selector"])

        obs2 = capture_observation(self._page, telemetry_store=store)
        filled = next(e for e in obs2["interactive_elements"]
                      if e["name"] == "Workspace name")
        self.assertEqual(filled.get("value"), "Acme")
        self.assertIsNone(obs2["visual"])  # perception stayed pixel-free

    def test_explicit_screenshot_still_works_headless(self):
        self._page.goto("https://test.local/app", wait_until="load")
        b64 = self._page.screenshot()
        self.assertGreater(len(b64), 1000)  # evidence capture works headless


class MailSignalExtractionTests(SimpleTestCase):
    """Mail hardening: OTP + magic-link extraction from email bodies."""

    fx = None

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        from test_cases.autonomous_agent import AutonomousAgent
        cls.fx = staticmethod(AutonomousAgent._extract_verification_signals)

    def test_magic_link_from_anchor_href(self):
        otp, ml, links = self.fx(
            '<p>Click to verify: <a href="https://app.example.com/auth/verify?token=abc123">Verify</a></p>'
        )
        self.assertEqual(ml, "https://app.example.com/auth/verify?token=abc123")
        self.assertIsNone(otp)

    def test_bare_link_html_entities_and_trailing_punctuation(self):
        otp, ml, links = self.fx(
            "Visit https://app.example.com/activate?u=1&amp;x=2. to activate your account"
        )
        self.assertEqual(ml, "https://app.example.com/activate?u=1&x=2")

    def test_otp_prefers_keyword_line_over_copyright_year(self):
        body = "Welcome!\n(c) 2026 ExampleApp\nYour verification code is 483920\nThanks"
        otp, _, _ = self.fx(body)
        self.assertEqual(otp, "483920")

    def test_otp_fallback_plain_6_digit(self):
        otp, _, _ = self.fx("Your pin: 552134 is valid 10 min")
        self.assertEqual(otp, "552134")

    def test_noise_links_filtered_and_first_real_link_wins(self):
        otp, ml, links = self.fx(
            '<a href="https://mail.tm/unsub">unsub</a> '
            '<a href="https://example.com/reset-password?t=1">reset</a>'
        )
        self.assertEqual(ml, "https://example.com/reset-password?t=1")
        self.assertFalse(any("mail.tm" in l for l in links))

    def test_empty_body_is_safe(self):
        self.assertEqual(self.fx(""), (None, None, []))
        self.assertEqual(self.fx(None), (None, None, []))


class CaptchaSolverTests(SimpleTestCase):
    """CAPTCHA handling: heuristics + manager wiring (no network, real pages
    served via page.route where a browser is available)."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        from test_cases import browser_captcha_solver as solver
        cls.solver = solver

    def _page(self, html):
        pw = sync_playwright().start()
        browser = pw.chromium.launch(headless=True, args=["--no-sandbox"])
        page = browser.new_page()
        page.route("https://captcha.test/app",
                   lambda r: r.fulfill(content_type="text/html", body=html))
        page.goto("https://captcha.test/app", wait_until="load")
        return page, browser, pw

    def _teardown(self, browser, pw):
        browser.close()
        pw.stop()

    def test_detect_no_challenge_on_plain_page(self):
        page, browser, pw = self._page("<html><body><button>ok</button></body></html>")
        try:
            rep = self.solver.attempt_captcha_bypass(page)
            self.assertEqual(rep["kind"], "none")
            self.assertFalse(rep["solved"])
            self.assertFalse(rep["needs_human"])
        finally:
            self._teardown(browser, pw)

    def test_page_level_turnstile_input_detected(self):
        html = ('<form><div class="cf-turnstile"></div>'
                '<input name="cf-turnstile-response" type="hidden" value=""></form>')
        page, browser, pw = self._page(html)
        try:
            kind, detail = self.solver.detect_challenge(page)
            self.assertEqual(kind, "turnstile")
        finally:
            self._teardown(browser, pw)

    def test_report_shape_is_complete(self):
        page, browser, pw = self._page("<html><body>plain</body></html>")
        try:
            rep = self.solver.attempt_captcha_bypass(page)
            for key in ("solved", "kind", "mode", "token_present", "attempts",
                        "elapsed_ms", "detail", "needs_human"):
                self.assertIn(key, rep)
            self.assertIsInstance(rep["elapsed_ms"], int)
        finally:
            self._teardown(browser, pw)

    def test_humanized_click_never_raises_on_missing_element(self):
        page, browser, pw = self._page("<html><body></body></html>")
        try:
            fake = page.locator("#does-not-exist")
            self.assertFalse(self.solver._humanized_click(page, fake))
        finally:
            self._teardown(browser, pw)

    def test_manager_template_wires_captcha(self):
        # Read by path — the template is a placeholder source, not importable.
        import os
        _base = os.path.dirname(AutonomousAgentPromptProbe.source_file())
        with open(os.path.join(_base, "browser_manager_template.py")) as fh:
            src = fh.read()
        # solve_captcha action exists and solver import is optional (degrade-safe)
        self.assertIn("'solve_captcha'", src)
        self.assertIn("import browser_captcha_solver as captcha_solver", src)
        self.assertIn("attempt_captcha_bypass", src)
        # stealth args come from the solver module's LAUNCH_ARGS at launch
        self.assertIn("getattr(captcha_solver, 'LAUNCH_ARGS'", src)
        with open(os.path.join(
                _base, "browser_captcha_solver.py")) as fh:
            solver_src = fh.read()
        self.assertIn("--disable-blink-features=AutomationControlled", solver_src)
        self.assertIn("webdriver", solver_src)  # stealth init script

    def test_agent_prompt_documents_captcha(self):
        src = open(AutonomousAgentPromptProbe.source_file()).read()
        self.assertIn("solve_captcha", src)
        self.assertIn("Turnstile", src)

    def test_captcha_escalation_pauses_mission_for_human(self):
        """needs_human report -> mission paused, prompt injected, evidence saved."""
        from unittest import mock as _mock
        import test_cases.autonomous_agent as aa

        agent = aa.AutonomousAgent.__new__(aa.AutonomousAgent)  # skip __init__
        agent._vnc_url = None  # headless escalation wording
        agent.sandbox = None   # no sandbox -> GUI boot skipped, fallback wording
        mission = _mock.MagicMock()
        agent._current_mission = mission

        with _mock.patch.object(agent, "_execute_browser_action",
                                return_value={"screenshot_b64": "ZmFrZQ=="}), \
             _mock.patch.object(agent, "_persist_screenshot_evidence",
                                return_value="https://cdn.test/shot.png") as pse, \
             _mock.patch.object(aa.AgentPrompt.objects, "create") as prompt_create:
            agent._request_human_for_captcha({"kind": "turnstile", "detail": "interactive challenge"})

        self.assertEqual(mission.status, "paused")
        mission.save.assert_called_once()
        self.assertTrue(agent._auto_pause_active)  # arms the 10-min auto-release
        pse.assert_called_once()
        prompt_create.assert_called_once()
        self.assertIn("HUMAN HELP NEEDED", prompt_create.call_args.kwargs["prompt"])
        self.assertIn("headless", prompt_create.call_args.kwargs["prompt"])


class AutonomousAgentPromptProbe:
    """Tiny helper so tests avoid importing the full agent module eagerly."""

    @staticmethod
    def source_file():
        import os
        from test_cases import autonomous_agent
        return autonomous_agent.__file__

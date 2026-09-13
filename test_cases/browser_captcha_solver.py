"""
browser_captcha_solver.py — free, self-hosted CAPTCHA handling for the agent's
browser manager. Uploaded into the E2B sandbox at mission runtime next to
browser_manager.py (same mechanism as browser_observation.py), so it must only
import stdlib + playwright.

Design (mirrors the Observation V2 layer):
  - Playwright owns ALL interaction (clicks, waits, frames). No second browser.
  - CDP is NOT used here — Turnstile detection reads Cloudflare's own widget
    state via the DOM/JS the widget already exposes.
  - Solvers are heuristics for the *non-interactive* tier of challenges:
      * Cloudflare Turnstile  — checkbox + managed/non-interactive challenges
      * reCAPTCHA v2          — the "I'm not a robot" checkbox only
      * hCaptcha              — the checkbox only
    Image/interactive challenges (puzzle grids) are DETECTED and reported as
    `needs_human=True` so the agent can pause for human takeover instead of
    failing the mission. No paid APIs, no third-party solver services.

Human-emulation:
  - `navigator.webdriver` and the automation switches are patched at context
    creation (see manager integration), which is what Turnstile actually
    fingerprints — not canvas noise games.
  - Checkbox clicks go through a jittered mouse path with human-ish timing.

Everything here is best-effort by design: `attempt_captcha_bypass` ALWAYS
returns a report dict (never raises), so the browser manager can merge it
into the observation and the agent can reason about the outcome.
"""

import base64
import random
import time

# --- Tunables (kwargs override for tests) --------------------------------
CF_SOLVE_TIMEOUT_S = 30      # explicit 'solve_captcha' budget (Turnstile can be slow)
AUTO_SOLVE_TIMEOUT_S = 8     # bounded post-navigate auto window — never stall a mission
CHECKBOX_TIMEOUT_S = 12      # simple checkbox widgets
POLL_INTERVAL_S = 0.7
CLICK_HUMANIZE_S = 0.6       # extra human-ish pre-click behavior

TURNSTILE_IFRAME_SEL = 'iframe[src*="challenges.cloudflare.com"]'
TURNSTILE_INPUT_SEL = 'input[name="cf-turnstile-response"], input[name="g-recaptcha-response"]'
RECAPTCHA_ANCHOR_FRAME = 'iframe[title*="recaptcha"][src*="google.com/recaptcha/api2/anchor"]'
HCAPTCHA_FRAME = 'iframe[src*="hcaptcha.com"]'
# Page-level Cloudflare interstitial ("Managed Challenge")
CF_INTERSTITIAL_SEL = '#challenge-stage, #challenge-form, #challenge-running, #challenge-error-text'

INTERACTIVE_CHALLENGE_HINTS = (
    'bframe',                     # reCAPTCHA image-challenge frame
    'challenge-container',        # hCaptcha task grid
    'task-image', 'hcaptcha-challenge',
)


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------

def detect_challenge(page):
    """Return (kind, detail) for the first challenge found on the page.

    kind: 'turnstile' | 'recaptcha_v2' | 'hcaptcha' | 'none'
    Never raises; cross-origin frame probing errors are swallowed per-frame.
    """
    try:
        # Page-level Turnstile (token input rendered directly on the page)
        if page.locator(TURNSTILE_INPUT_SEL).count() > 0:
            return 'turnstile', 'cf widget on page'
        if page.locator(TURNSTILE_IFRAME_SEL).count() > 0:
            return 'turnstile', 'cf iframe widget'
        # Same-origin managed challenge interstitial
        if page.locator(CF_INTERSTITIAL_SEL).count() > 0:
            return 'turnstile', 'managed challenge interstitial'
        if page.locator(RECAPTCHA_ANCHOR_FRAME).count() > 0:
            return 'recaptcha_v2', 'anchor checkbox'
        if page.locator(HCAPTCHA_FRAME).count() > 0:
            return 'hcaptcha', 'checkbox'
    except Exception:  # noqa: BLE001 — page may be mid-navigation
        pass
    return 'none', ''


def _iter_challenge_frames(page, src_sub):
    """Yield frames whose URL contains `src_sub` (e.g. challenges.cloudflare.com)."""
    try:
        for frame in page.frames:
            try:
                if src_sub in (frame.url or ''):
                    yield frame
            except Exception:  # noqa: BLE001
                continue
    except Exception:  # noqa: BLE001
        return


def _token_present(page):
    """True if a turnstile/recaptcha response input has a token value."""
    try:
        for loc in (page.locator(TURNSTILE_INPUT_SEL),):
            for i in range(loc.count()):
                try:
                    val = loc.nth(i).input_value()
                    if val and len(val) > 20:
                        return True
                except Exception:  # noqa: BLE001
                    continue
        for frame in _iter_challenge_frames(page, 'challenges.cloudflare.com'):
            try:
                for i in range(frame.locator(TURNSTILE_INPUT_SEL).count()):
                    try:
                        val = frame.locator(TURNSTILE_INPUT_SEL).nth(i).input_value()
                        if val and len(val) > 20:
                            return True
                    except Exception:  # noqa: BLE001
                        continue
            except Exception:  # noqa: BLE001
                continue
    except Exception:  # noqa: BLE001
        pass
    return False


def _turnstile_widget_success(frame):
    """Probe one Cloudflare widget iframe for the success state."""
    try:
        # The checkbox label flips to a success state (checkmark classes)
        for sel in ('[class*="success"]', '[aria-checked="true"]'):
            if frame.locator(sel).count() > 0:
                return True
        # Token injected inside the widget frame
        tok = frame.locator(TURNSTILE_INPUT_SEL)
        for i in range(tok.count()):
            try:
                if len(tok.nth(i).input_value() or '') > 20:
                    return True
            except Exception:  # noqa: BLE001
                continue
    except Exception:  # noqa: BLE001
        pass
    return False


def _interstitial_cleared(page, base_url):
    """True once a same-origin managed challenge has let us through."""
    try:
        if page.locator(CF_INTERSTITIAL_SEL).count() > 0:
            return False
        title = (page.title() or '').lower()
        if any(t in title for t in ('just a moment', 'attention required', 'checking your browser')):
            return False
        # Navigated away from the challenge host path is the strongest signal
        if 'challenges.cloudflare.com' in (page.url or '') and base_url not in (page.url or ''):
            return False
        return True
    except Exception:  # noqa: BLE001
        return False


# ---------------------------------------------------------------------------
# Human-ish interaction
# ---------------------------------------------------------------------------

def _humanized_click(page, loc):
    """Click with a jittered mouse path; falls back to a plain click."""
    try:
        box = loc.bounding_box()
        if box:
            tx = box['x'] + box['width'] * random.uniform(0.35, 0.65)
            ty = box['y'] + box['height'] * random.uniform(0.35, 0.65)
            page.mouse.move(tx - random.randint(80, 200), ty - random.randint(40, 120))
            for _ in range(random.randint(4, 7)):
                tx_j = tx + random.randint(-3, 3)
                ty_j = ty + random.randint(-3, 3)
                page.mouse.move(tx_j, ty_j)
                time.sleep(random.uniform(0.02, 0.06))
            time.sleep(random.uniform(0.08, 0.2))
            page.mouse.down()
            time.sleep(random.uniform(0.04, 0.09))
            page.mouse.up()
            return True
    except Exception:  # noqa: BLE001
        pass
    try:
        loc.click(timeout=3000)
        return True
    except Exception:  # noqa: BLE001
        return False


# ---------------------------------------------------------------------------
# Solvers
# ---------------------------------------------------------------------------

def solve_turnstile(page, max_wait_s=CF_SOLVE_TIMEOUT_S, poll_interval_s=POLL_INTERVAL_S):
    """Cloudflare Turnstile: click checkbox widgets, wait out managed challenges.

    Managed / non-interactive challenges usually resolve by themselves once the
    browser looks human (see stealth init script in the manager). Checkbox
    widgets need one humanized click inside the CF iframe.
    """
    start = time.time()
    clicked = False
    base_url = page.url
    attempts = 0
    detail = 'waiting for widget'

    while time.time() - start < max_wait_s:
        # 1) Token already present anywhere? → solved
        if _token_present(page):
            return {'solved': True, 'kind': 'turnstile', 'mode': 'token',
                    'token_present': True, 'attempts': attempts,
                    'elapsed_ms': int((time.time() - start) * 1000),
                    'detail': 'cf-turnstile-response token present', 'needs_human': False}

        # 2) Checkbox widget inside a CF iframe → click once (humanized)
        for frame in _iter_challenge_frames(page, 'challenges.cloudflare.com'):
            try:
                box_sel = ('label.ctp-checkbox-label, input[type="checkbox"], '
                           '[role="checkbox"], .ctp-checkbox-label')
                loc = frame.locator(box_sel).first
                if loc.count() > 0 and not clicked:
                    if _humanized_click(page, loc):
                        clicked = True
                        attempts += 1
                        detail = 'clicked cf checkbox'
                    time.sleep(CLICK_HUMANIZE_S)
                if _turnstile_widget_success(frame):
                    return {'solved': True, 'kind': 'turnstile', 'mode': 'checkbox',
                            'token_present': _token_present(page), 'attempts': attempts,
                            'elapsed_ms': int((time.time() - start) * 1000),
                            'detail': 'widget reports success', 'needs_human': False}
            except Exception:  # noqa: BLE001
                continue

        # 3) Same-origin managed challenge → wait for the interstitial to clear
        if page.locator(CF_INTERSTITIAL_SEL).count() > 0:
            if _interstitial_cleared(page, base_url):
                return {'solved': True, 'kind': 'turnstile', 'mode': 'managed_challenge',
                        'token_present': False, 'attempts': attempts,
                        'elapsed_ms': int((time.time() - start) * 1000),
                        'detail': 'interstitial cleared', 'needs_human': False}
        elif clicked or attempts > 0 or _turnstile_iframe_seen(page):
            # No interstitial and no widget anymore → page moved past the gate
            if page.locator(TURNSTILE_IFRAME_SEL).count() == 0 and not _token_present(page):
                kind2, _ = detect_challenge(page)
                if kind2 == 'none':
                    return {'solved': True, 'kind': 'turnstile', 'mode': 'managed_challenge',
                            'token_present': False, 'attempts': attempts,
                            'elapsed_ms': int((time.time() - start) * 1000),
                            'detail': 'widget no longer present', 'needs_human': False}

        time.sleep(poll_interval_s)

    # Detect an interactive (image) Turnstile sub-challenge → hand to human
    needs_human, hdet = _interactive_challenge_present(page)
    return {'solved': False, 'kind': 'turnstile', 'mode': 'unknown',
            'token_present': _token_present(page), 'attempts': attempts,
            'elapsed_ms': int((time.time() - start) * 1000),
            'detail': f'timeout: {detail or hdet}', 'needs_human': needs_human}


def _turnstile_iframe_seen(page):
    try:
        return page.locator(TURNSTILE_IFRAME_SEL).count() > 0
    except Exception:  # noqa: BLE001
        return False


def _interactive_challenge_present(page):
    """Image/puzzle challenges cannot be solved without vision or a human."""
    try:
        for frame in page.frames:
            u = (frame.url or '').lower()
            if any(h in u for h in INTERACTIVE_CHALLENGE_HINTS):
                return True, f'interactive challenge frame: {u[:80]}'
    except Exception:  # noqa: BLE001
        pass
    return False, ''


def _solve_checkbox_frame(page, frame_sel, checkbox_sel, kind,
                          max_wait_s=CHECKBOX_TIMEOUT_S, poll_interval_s=POLL_INTERVAL_S):
    """Generic checkbox-widget solver (reCAPTCHA anchor / hCaptcha)."""
    start = time.time()
    attempts = 0
    last_error = 'widget not found'

    while time.time() - start < max_wait_s:
        try:
            if kind == 'recaptcha_v2':
                frame = page.frame_locator(frame_sel)
                anchor = frame.locator(checkbox_sel).first
                try:
                    checked = anchor.get_attribute('aria-checked')
                    if checked == 'true':
                        return {'solved': True, 'kind': kind, 'mode': 'checkbox',
                                'token_present': True, 'attempts': attempts,
                                'elapsed_ms': int((time.time() - start) * 1000),
                                'detail': 'anchor aria-checked=true', 'needs_human': False}
                except Exception:  # noqa: BLE001
                    pass
                if anchor.count() > 0 and attempts == 0:
                    if _humanized_click(page, anchor):
                        attempts += 1
                        last_error = 'clicked, awaiting state'
                        time.sleep(CLICK_HUMANIZE_S)
            else:  # hcaptcha
                frame = page.frame_locator(frame_sel)
                cb = frame.locator(checkbox_sel).first
                if cb.count() > 0 and attempts == 0:
                    if _humanized_click(page, cb):
                        attempts += 1
                        last_error = 'clicked, awaiting state'
                        time.sleep(CLICK_HUMANIZE_S)
                # hCaptcha success: checkbox gets aria-checked / check icon
                try:
                    if cb.get_attribute('aria-checked') == 'true':
                        return {'solved': True, 'kind': kind, 'mode': 'checkbox',
                                'token_present': True, 'attempts': attempts,
                                'elapsed_ms': int((time.time() - start) * 1000),
                                'detail': 'checkbox checked', 'needs_human': False}
                except Exception:  # noqa: BLE001
                    pass
                # Solved widgets also inject a hidden response textarea
                if _token_present(page):
                    return {'solved': True, 'kind': kind, 'mode': 'checkbox',
                            'token_present': True, 'attempts': attempts,
                            'elapsed_ms': int((time.time() - start) * 1000),
                            'detail': 'response token present', 'needs_human': False}
        except Exception as e:  # noqa: BLE001
            last_error = str(e)[:120]

        # Interactive image challenge appeared → stop, escalate to human
        needs_human, hdet = _interactive_challenge_present(page)
        if needs_human:
            return {'solved': False, 'kind': kind, 'mode': 'interactive',
                    'token_present': False, 'attempts': attempts,
                    'elapsed_ms': int((time.time() - start) * 1000),
                    'detail': hdet, 'needs_human': True}

        time.sleep(poll_interval_s)

    return {'solved': False, 'kind': kind, 'mode': 'checkbox',
            'token_present': False, 'attempts': attempts,
            'elapsed_ms': int((time.time() - start) * 1000),
            'detail': f'timeout: {last_error}',
            'needs_human': True}


def solve_recaptcha_v2(page, max_wait_s=CHECKBOX_TIMEOUT_S, poll_interval_s=POLL_INTERVAL_S):
    """reCAPTCHA v2 'I'm not a robot' checkbox only. Image grids → needs_human."""
    return _solve_checkbox_frame(page, RECAPTCHA_ANCHOR_FRAME, '#recaptcha-anchor',
                                 'recaptcha_v2', max_wait_s, poll_interval_s)


def solve_hcaptcha(page, max_wait_s=CHECKBOX_TIMEOUT_S, poll_interval_s=POLL_INTERVAL_S):
    """hCaptcha checkbox only. Task grids → needs_human."""
    return _solve_checkbox_frame(page, HCAPTCHA_FRAME, '#checkbox',
                                 'hcaptcha', max_wait_s, poll_interval_s)


# ---------------------------------------------------------------------------
# Entry point (never raises)
# ---------------------------------------------------------------------------

def attempt_captcha_bypass(page, kind=None, max_wait_s=None, poll_interval_s=POLL_INTERVAL_S):
    """Detect and attempt to bypass a CAPTCHA on the current page.

    Returns a report dict:
      {solved, kind, mode, token_present, attempts, elapsed_ms, detail, needs_human}
    kind='none' means no challenge was detected (cheap, <0.5s).
    """
    t0 = time.time()
    try:
        k, detail = detect_challenge(page) if not kind else (kind, '')
        if k == 'none':
            return {'solved': False, 'kind': 'none', 'mode': None,
                    'token_present': False, 'attempts': 0,
                    'elapsed_ms': int((time.time() - t0) * 1000),
                    'detail': 'no challenge detected', 'needs_human': False}

        if k == 'turnstile':
            return solve_turnstile(
                page,
                max_wait_s=max_wait_s if max_wait_s is not None else CF_SOLVE_TIMEOUT_S,
                poll_interval_s=poll_interval_s)
        if k == 'recaptcha_v2':
            return solve_recaptcha_v2(
                page, max_wait_s=max_wait_s if max_wait_s is not None else CHECKBOX_TIMEOUT_S,
                poll_interval_s=poll_interval_s)
        if k == 'hcaptcha':
            return solve_hcaptcha(
                page, max_wait_s=max_wait_s if max_wait_s is not None else CHECKBOX_TIMEOUT_S,
                poll_interval_s=poll_interval_s)

        return {'solved': False, 'kind': k, 'mode': None, 'token_present': False,
                'attempts': 0, 'elapsed_ms': int((time.time() - t0) * 1000),
                'detail': detail or f'unsupported kind: {k}', 'needs_human': True}
    except Exception as e:  # noqa: BLE001 — must never kill an action
        return {'solved': False, 'kind': 'error', 'mode': None, 'token_present': False,
                'attempts': 0, 'elapsed_ms': int((time.time() - t0) * 1000),
                'detail': f'solver error: {str(e)[:150]}', 'needs_human': True}


def diagnose_captcha(page):
    """Detailed introspection for an explicit 'solve_captcha' with diagnose=1.

    Deliberately does NOT attempt a solve — diagnosis must stay fast; the
    agent calls this to decide strategy, not to burn the solve budget.
    """
    kind, detail = detect_challenge(page)
    report = {'solved': False, 'kind': kind, 'mode': 'diagnostic',
              'token_present': _token_present(page), 'attempts': 0,
              'elapsed_ms': 0, 'detail': detail or 'no solve attempted',
              'needs_human': kind not in ('none', 'turnstile', 'recaptcha_v2', 'hcaptcha')}
    try:
        report['diagnosis'] = {
            'url': page.url,
            'title': page.title(),
            'turnstile_inputs': page.locator(TURNSTILE_INPUT_SEL).count(),
            'cf_iframes': page.locator(TURNSTILE_IFRAME_SEL).count(),
            'cf_interstitial': page.locator(CF_INTERSTITIAL_SEL).count(),
            'recaptcha_anchor': page.locator(RECAPTCHA_ANCHOR_FRAME).count(),
            'hcaptcha_frames': page.locator(HCAPTCHA_FRAME).count(),
            'interactive_hint': _interactive_challenge_present(page)[0],
            'frames': [f.url[:120] for f in page.frames if f.url and f.url != 'about:blank'][:10],
        }
    except Exception as e:  # noqa: BLE001
        report['diagnosis'] = {'error': str(e)[:150]}
    return report


# Init script injected at context creation (before any navigation). This is the
# part Turnstile actually fingerprints; keep it minimal and honest.
STEALTH_INIT_SCRIPT = """
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
"""

LAUNCH_ARGS = [
    '--no-sandbox', '--disable-gpu', '--disable-dev-shm-usage',
    '--disable-blink-features=AutomationControlled',
]

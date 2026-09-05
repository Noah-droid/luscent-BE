import json
import base64
import sys
import os
import time
import traceback

# Force global browser configs inside the python process
os.environ['PLAYWRIGHT_BROWSERS_PATH'] = '/ms-playwright'
__DISPLAY_LINE__

# Redirect stderr to a file so we can debug launch failures
sys.stderr = open('/tmp/browser_manager.err', 'w')
sys.stderr.write('Browser manager starting (headless=__HEADLESS__)\n')
sys.stderr.flush()

from playwright.sync_api import sync_playwright


def run():
    try:
        with sync_playwright() as p:
            # Launch browser — non-headless when VNC is active (visible in framebuffer)
            sys.stderr.write('Launching Chromium (headless=__HEADLESS__)...\n')
            sys.stderr.flush()
            browser = p.chromium.launch(
                headless=__HEADLESS__,
                args=['--no-sandbox', '--disable-gpu', '--disable-dev-shm-usage']
            )
            sys.stderr.write('Chromium launched OK\n')
            sys.stderr.flush()
            context = browser.new_context(viewport={'width': 1280, 'height': 800})
            page = context.new_page()

            # Navigate to a blank page so the user sees *something* in VNC immediately
            page.goto('about:blank')
            sys.stderr.write('Browser ready on about:blank\n')
            sys.stderr.flush()

        # Signal ready — the browser window is now visible in the VNC stream
        print("READY", flush=True)

        for line in sys.stdin:
            if not line.strip():
                continue
            try:
                action = json.loads(line)
                action_type = action.get('action')

                if action_type == 'navigate':
                    page.goto(action.get('url'), wait_until='domcontentloaded')
                elif action_type == 'click':
                    page.click(action.get('selector'))
                elif action_type == 'type':
                    page.fill(action.get('selector'), str(action.get('value')))
                elif action_type == 'scroll':
                    page.evaluate('window.scrollBy(0, arguments[0])', action.get('pixels', 500))
                elif action_type == 'wait':
                    page.wait_for_timeout(action.get('ms', 2000))

                page.wait_for_timeout(500)

                # Capture result + screenshot for the agent's visual observation
                screenshot = base64.b64encode(page.screenshot()).decode('utf-8')
                print(json.dumps({
                    "status": "success",
                    "title": page.title(),
                    "url": page.url,
                    "screenshot_b64": screenshot,
                    "html_preview": page.content()[:500]
                }), flush=True)
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

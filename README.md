# Luscent — AI-Powered QA Agent

Luscent is an automated QA agent that tests web applications, detects regressions, and reports failures before they reach production.

It simulates real user behavior, executes test flows, and generates actionable reports with logs and screenshots.

Built for fast-moving teams that ship frequently and need confidence in every release.

---

## Why Luscent exists

Modern teams ship fast. Manual QA slows releases and still misses critical issues.

Luscent solves this by automatically:

- Executing real user flows
- Detecting UI and functional regressions
- Capturing failures with screenshots and logs
- Providing clear test reports

This reduces production bugs and increases release confidence.

---

## Core Features

- Automated browser testing using Playwright
- User flow execution (login, navigation, actions)
- Regression detection
- Screenshot capture on failure
- Structured test reports
- Headless execution (CI/CD compatible)
- CLI-based test runner

Planned:

- AI-generated test scenarios
- Self-healing selectors
- CI/CD integrations
- Dashboard UI

## Sandbox Infrastructure (E2B)

Luscent uses **E2B Sandboxes** (Firecracker MicroVMs) to execute tests in a secure, isolated, and stateful environment. This allows agents to maintain session state (like logins) across multiple test runs.

### 1. Environment Variables

Add the following to your `.env` file:

```bash
E2B_API_KEY=e2b_your_api_key_here
E2B_SANDBOX_TEMPLATE=qai-runner
```

### 2. Developing & Building the Template

The sandbox environment is defined in `e2b_runner/Dockerfile`. Every time you update system-level dependencies (like Playwright or Python packages), you must rebuild the template.

**One-time CLI Setup:**

```bash
npm install -g @e2b/cli
e2b auth login
```

**Build the Template (Cloud Build):**

```bash
cd e2b_runner/qai-runner
export E2B_API_KEY=your_api_key_here
npm run e2b:build:dev
```

_Note: This uses E2B's Cloud Build system, so local Docker is not required._

### 3. Stateful Agents (Sessions)

To enable session persistence (keeping the sandbox alive between tests):

1. Create a `batch_id` (UUID) for your test sequence.
2. Set `keep_alive=True` on the `TestCase` model.
3. The `RunnerService` will automatically detect active sandboxes in the same batch and attach to them.

---

## Getting Started (Local Development)

1. **Install Backend Dependencies:**

   ```bash
   python -m venv env
   source env/bin/activate
   pip install -r requirements.txt
   ```

2. **Run Migrations:**

   ```bash
   python manage.py migrate
   ```

3. **Start Celery (for async tests):**
   ```bash
   celery -A config worker --loglevel=info
   ```

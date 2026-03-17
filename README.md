# Sierra

FastAPI service that listens for Sentry webhooks (errors created or issues escalated). When an **unhandled** exception is detected, it launches a Cursor Cloud Agent with full error context to automatically fix it.

## Setup

```bash
pip install -e .
cp .env.example .env
# Edit .env with your values
```

## Environment Variables

| Variable                | Required | Description                                                                                    |
| ----------------------- | -------- | ---------------------------------------------------------------------------------------------- |
| `CURSOR_API_KEY`        | Yes      | API key from [Cursor Dashboard](https://cursor.com/settings)                                   |
| `CURSOR_MODEL`          | No       | Model for Cloud Agent (default: `cursor-composer-1-5`)                                         |
| `GITHUB_REPOSITORY`     | Yes\*    | GitHub repo URL (e.g. `https://github.com/org/repo`)                                           |
| `GITHUB_REF`            | No       | Branch/base ref (default: `main`)                                                              |
| `CURSOR_AUTO_CREATE_PR` | No       | Auto-create PR on fix (default: `true`)                                                        |
| `REDIS_URL`             | No       | Redis connection URL (default: `redis://localhost:6379/0`)                                     |
| `SENTRY_WEBHOOK_SECRET` | Yes      | Client secret from Sentry integration (verifies webhook authenticity)                          |
| `SENTRY_AUTH_TOKEN`     | Yes\*    | Bearer token for Sentry API (for manual trigger from dashboard)                                |
| `DASHBOARD_PASSWORD`    | Yes      | Password for dashboard access                                                                  |
| `SENTRY_PROJECT_<slug>` | No       | Map Sentry project slug to repo (e.g. `SENTRY_PROJECT_backend=https://github.com/org/backend`) |

\* Or configure per-project mapping via `SENTRY_PROJECT_<slug>`.

## Sentry Webhook Configuration

1. In Sentry: **Settings → Integrations → Webhooks** (or install a custom integration)
2. Add your webhook URL: `https://your-host/webhooks/sentry`
3. Subscribe to **Errors** and **Issues**
4. Sentry sends `Sentry-Hook-Resource: error` or `Sentry-Hook-Resource: issue`

**Note:** Error webhooks require Sentry Business or Enterprise.

## Behavior

- **Error created**: Unhandled exceptions (`mechanism.handled: false` or `handled: no` tag) trigger an agent
- **Issue escalated**: When `substatus` is `escalating` or `isUnhandled` is true
- Agent receives: error title, stack trace, context lines, metadata
- Agent creates a branch and (optionally) a PR with the fix

## Run

**Local:**

```bash
uvicorn app.main:app --reload
```

**Docker (app + Redis):**

```bash
cp .env.example .env   # Add your keys
docker compose up --build
```

Dashboard: `http://localhost:8000` — shows recent triggered runs with links to Sentry and Cursor agent. Paste a Sentry error URL to manually trigger a fix (requires `SENTRY_AUTH_TOKEN`). Entries older than 7 days are automatically hidden.

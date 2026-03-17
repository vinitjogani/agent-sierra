import html
import json
import logging
import secrets
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, Request, Response
from fastapi.exceptions import HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from app.auth import RequireAuth, clear_session, set_session_cookie, verify_sentry_webhook
from app.redis_store import get_recent_runs
from app.sentry_client import fetch_and_trigger
from app.sentry_webhook import handle_sentry_webhook

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="Agent Sierra", description="Sentry → Cursor Cloud Agent")

TEMPLATE_DIR = Path(__file__).parent / "templates"


@app.exception_handler(HTTPException)
def _http_exception_handler(request: Request, exc: HTTPException):
    if exc.status_code == 302 and "Location" in (exc.headers or {}):
        return RedirectResponse(url=exc.headers["Location"], status_code=302)
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})


def _escape(s: str) -> str:
    return html.escape(s or "", quote=True)


def _fmt_ts(ts: float | None) -> str:
    if ts is None:
        return ""
    try:
        return datetime.utcfromtimestamp(ts).strftime("%Y-%m-%d %H:%M")
    except (TypeError, ValueError):
        return ""


@app.get("/login", response_class=HTMLResponse)
async def login_page():
    body = (TEMPLATE_DIR / "login.html").read_text().replace("{{error}}", "")
    return HTMLResponse(body)


@app.post("/login")
async def login(request: Request, response: Response):
    from app.config import get_config

    password = get_config().get("dashboard_password")
    if not password:
        raise HTTPException(status_code=503, detail="DASHBOARD_PASSWORD not configured")
    form = await request.form()
    if secrets.compare_digest(form.get("password", ""), password):
        set_session_cookie(response, password)
        return RedirectResponse("/", status_code=302)
    body = (TEMPLATE_DIR / "login.html").read_text().replace("{{error}}", "<p class=\"error\">Invalid password</p>")
    return HTMLResponse(body, status_code=401)


@app.get("/logout")
async def logout(response: Response):
    clear_session(response)
    return RedirectResponse("/login", status_code=302)


@app.get("/", response_class=HTMLResponse)
async def dashboard(_: None = RequireAuth):
    runs = get_recent_runs()
    if runs:
        rows = "".join(
            f'<tr><td>{_escape(r.get("title", ""))}</td>'
            f'<td><a href="{_escape(r.get("sentry_url") or "#")}" target="_blank">Sentry</a></td>'
            f'<td><a href="{_escape(r.get("cursor_url") or "#")}" target="_blank">Agent</a></td>'
            f'<td>{_fmt_ts(r.get("created_at"))}</td></tr>'
            for r in runs
        )
    else:
        rows = '<tr><td colspan="4" class="meta">No runs yet. Triggered agents will appear here.</td></tr>'
    body = (TEMPLATE_DIR / "dashboard.html").read_text()
    return HTMLResponse(body.replace("{{rows}}", rows).replace("{{count}}", str(len(runs))))


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/api/runs")
async def api_runs(_: None = RequireAuth):
    return {"runs": get_recent_runs()}


@app.post("/api/trigger")
async def manual_trigger(request: Request, _: None = RequireAuth):
    try:
        body = await request.json()
        url = (body.get("sentry_url") or "").strip()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")
    if not url:
        raise HTTPException(status_code=400, detail="sentry_url required")
    result = fetch_and_trigger(url)
    return result


@app.post("/webhooks/sentry")
async def sentry_webhook(request: Request):
    resource = request.headers.get("Sentry-Hook-Resource", "").lower()
    if resource not in ("error", "issue"):
        return JSONResponse(status_code=400, content={"error": "Missing or invalid Sentry-Hook-Resource header"})
    body = await request.body()
    verify_sentry_webhook(request, body)
    try:
        payload = json.loads(body)
    except Exception as e:
        logger.warning("Invalid webhook body: %s", e)
        return JSONResponse(status_code=400, content={"error": "Invalid JSON"})
    result = handle_sentry_webhook(payload, resource)
    logger.info("Sentry webhook %s: %s", resource, result)
    return JSONResponse(content=result)

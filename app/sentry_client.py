import logging
import re
from typing import Any

import httpx

from app.config import get_config

logger = logging.getLogger(__name__)

SENTRY_API = "https://sentry.io/api/0"


def _parse_sentry_url(url: str) -> tuple[str | None, str | None, str]:
    url = url.strip()
    if not url.startswith("http"):
        url = "https://" + url
    org = None
    issue_id = None
    event_id = "latest"
    if m := re.search(r"organizations/([^/]+)/issues/(\d+)(?:/events/([^/]+))?", url):
        org, issue_id, event_id = m.group(1), m.group(2), m.group(3) or "latest"
    elif m := re.search(r"([a-zA-Z0-9-]+)\.sentry\.io/issues/(\d+)(?:/events/([^/?#]+))?", url):
        org, issue_id, event_id = m.group(1), m.group(2), m.group(3) or "latest"
    return org, issue_id, event_id


def _normalize_frame(frame: dict[str, Any]) -> dict[str, Any]:
    result = {
        "filename": frame.get("filename") or frame.get("absPath", "?"),
        "abs_path": frame.get("absPath") or frame.get("filename"),
        "lineno": frame.get("lineNo") or frame.get("lineno", "?"),
        "function": frame.get("function", "?"),
    }
    if frame.get("context_line") is not None:
        result["context_line"] = str(frame["context_line"]).strip()
    if frame.get("pre_context"):
        result["pre_context"] = [str(l).rstrip() for l in frame["pre_context"]]
    if frame.get("post_context"):
        result["post_context"] = [str(l).rstrip() for l in frame["post_context"]]
    if frame.get("colno") is not None:
        result["colno"] = frame["colno"]
    if frame.get("in_app") is not None:
        result["in_app"] = frame["in_app"]
    if frame.get("vars"):
        result["vars"] = frame["vars"]
    ctx = frame.get("context") or []
    target_ln = result.get("lineno")
    if ctx and "context_line" not in result and target_ln is not None:
        sorted_ctx = sorted((c for c in ctx if len(c) >= 2), key=lambda x: (x[0] is None, x[0] or 0))
        pre_context, context_line, post_context = [], "", []
        for item in sorted_ctx:
            ln, line = item[0], str(item[1]).rstrip() if len(item) >= 2 and item[1] else ""
            if ln == target_ln:
                context_line = line.strip()
            elif ln is not None:
                (pre_context if ln < target_ln else post_context).append(line)
        result["context_line"] = context_line
        if pre_context:
            result["pre_context"] = pre_context
        if post_context:
            result["post_context"] = post_context
    elif ctx and "context_line" not in result:
        for item in ctx:
            if len(item) >= 2 and item[0] == target_ln:
                result["context_line"] = str(item[1]).strip() if item[1] else ""
                break
    result.setdefault("context_line", "")
    return result


def _event_to_webhook_payload(event: dict[str, Any], issue: dict[str, Any], sentry_url: str) -> dict[str, Any]:
    tags = {t["key"]: t["value"] for t in event.get("tags") or []}
    level = tags.get("level", "error")
    exc_values = []
    for entry in event.get("entries") or []:
        if entry.get("type") != "exception":
            continue
        for val in entry.get("data", {}).get("values") or []:
            stack = val.get("stacktrace") or {}
            frames = [_normalize_frame(f) for f in stack.get("frames") or []]
            exc_values.append({
                "type": val.get("type", "Error"),
                "value": val.get("value", ""),
                "mechanism": {"handled": tags.get("handled", "yes") == "no"},
                "stacktrace": {"frames": frames},
            })
    project = issue.get("project") or {}
    slug = project.get("slug") if isinstance(project, dict) else None
    metadata = event.get("metadata") or {}
    return {
        "data": {
            "error": {
                "title": event.get("title", issue.get("title", "Unknown")),
                "level": level,
                "platform": event.get("platform", "unknown"),
                "web_url": sentry_url,
                "exception": {"values": exc_values} if exc_values else {},
                "metadata": metadata,
                "project": {"slug": slug} if slug else project,
                "event_id": event.get("eventID") or event.get("id"),
            }
        }
    }


def fetch_and_trigger(sentry_url: str) -> dict[str, Any]:
    from app.cursor_client import launch_agent
    from app.redis_store import record_run
    from app.sentry_webhook import _format_error_prompt, _resolve_repository

    config = get_config()
    token = config.get("sentry_auth_token")
    if not token:
        return {"status": "error", "reason": "SENTRY_AUTH_TOKEN not configured"}

    org, issue_id, event_id = _parse_sentry_url(sentry_url)
    if not org or not issue_id:
        return {"status": "error", "reason": "Invalid Sentry URL format"}

    headers = {"Authorization": f"Bearer {token}"}
    try:
        issue_resp = httpx.get(
            f"{SENTRY_API}/organizations/{org}/issues/{issue_id}/",
            headers=headers,
            timeout=30.0,
        )
        issue_resp.raise_for_status()
        issue = issue_resp.json()

        event_resp = httpx.get(
            f"{SENTRY_API}/organizations/{org}/issues/{issue_id}/events/{event_id}/",
            headers=headers,
            timeout=30.0,
        )
        event_resp.raise_for_status()
        event = event_resp.json()
    except httpx.HTTPStatusError as e:
        logger.warning("Sentry API error: %s %s", e.response.status_code, e.response.text)
        return {"status": "error", "reason": f"Sentry API error: {e.response.status_code}"}
    except Exception as e:
        logger.exception("Failed to fetch Sentry data: %s", e)
        return {"status": "error", "reason": str(e)}

    payload = _event_to_webhook_payload(event, issue, sentry_url)
    proj = payload["data"]["error"].get("project")
    slug = proj.get("slug") if isinstance(proj, dict) else str(proj) if proj else None
    repo = _resolve_repository(slug)
    if not repo:
        return {"status": "error", "reason": "no repository configured for this project"}

    prompt = _format_error_prompt(payload)
    source = {"repository": repo, "ref": config["github_ref"]}
    target = {}
    if config["cursor_auto_create_pr"]:
        target["autoCreatePr"] = True
        eid = payload["data"]["error"].get("event_id") or issue.get("id") or ""
        target["branchName"] = f"cursor/fix-sentry-{str(eid).replace('-', '')[:8] or 'fix'}"

    result = launch_agent(prompt, source, target or None)
    if result:
        agent_url = result.get("target", {}).get("url", "")
        title = payload["data"]["error"].get("title", "Unknown")
        record_run(sentry_url, agent_url, title)
        return {"status": "triggered", "agent_id": result.get("id"), "agent_url": agent_url}
    return {"status": "error", "reason": "failed to launch Cursor agent"}

import json
import logging
from typing import Any

from app.config import get_config
from app.cursor_client import launch_agent
from app.redis_store import record_run

logger = logging.getLogger(__name__)


def _is_unhandled_error(payload: dict[str, Any]) -> bool:
    error = payload.get("data", {}).get("error", {})
    tags = dict(error.get("tags") or [])
    if tags.get("handled") == "no":
        return True
    for exc in (error.get("exception") or {}).get("values") or []:
        if (exc.get("mechanism") or {}).get("handled") is False:
            return True
    return False


def _is_unhandled_issue(payload: dict[str, Any]) -> bool:
    issue = payload.get("data", {}).get("issue", {})
    substatus = issue.get("substatus")
    return (
        issue.get("isUnhandled") is True
        or substatus == "escalating"
        or substatus == "regressed"
    )


def _resolve_repository(project_slug: str | None) -> str | None:
    config = get_config()
    if project_slug:
        repo = config["project_mapping"].get(project_slug.lower())
        if repo:
            return repo
    return config["github_repository"]


def _format_frame(frame: dict[str, Any], idx: int) -> list[str]:
    fn = frame.get("filename") or frame.get("abs_path") or "?"
    ln = frame.get("lineno", "?")
    col = frame.get("colno")
    func = frame.get("function") or "?"
    in_app = frame.get("in_app")
    loc = f"{fn}:{ln}" + (f":{col}" if col is not None else "")
    lines = [f"### Frame {idx}: {loc} in `{func}`"]
    if in_app is not None:
        lines.append(f"  (in_app={in_app})")
    pre = frame.get("pre_context") or []
    ctx_line = (frame.get("context_line") or "").strip()
    post = frame.get("post_context") or []
    ln_int = ln if isinstance(ln, int) else None
    if pre or ctx_line or post:
        start_ln = (ln_int - len(pre)) if ln_int is not None else 1
        code_lines = []
        for i, pc in enumerate(pre):
            num = start_ln + i if ln_int is not None else i + 1
            code_lines.append(f"{num: 4d} | {str(pc).rstrip()}")
        if ctx_line:
            err_ln = ln_int if ln_int is not None else start_ln + len(pre)
            code_lines.append(f"{err_ln: 4d} | {ctx_line}  <-- error line")
        for i, p in enumerate(post):
            num = (ln_int + 1 + i) if ln_int is not None else start_ln + len(pre) + 1 + i
            code_lines.append(f"{num: 4d} | {str(p).rstrip()}")
        if code_lines:
            lines.append("```")
            lines.extend(code_lines)
            lines.append("```")
    elif ctx_line:
        lines.append(f"  Line {ln}: `{ctx_line}`")
    vars_map = frame.get("vars")
    if vars_map and isinstance(vars_map, dict):
        lines.append("  **Local variables:**")
        for k, v in list(vars_map.items())[:20]:
            val_str = str(v)
            if len(val_str) > 200:
                val_str = val_str[:200] + "..."
            lines.append(f"    - {k}: {val_str}")
    return lines


def _format_error_prompt(payload: dict[str, Any]) -> str:
    error = payload.get("data", {}).get("error", {})
    lines = [
        "Fix the following unhandled exception reported by Sentry.",
        "",
        f"**Error**: {error.get('title', 'Unknown')}",
        f"**Level**: {error.get('level', 'error')}",
        f"**Platform**: {error.get('platform', 'unknown')}",
        f"**Sentry URL**: {error.get('web_url', 'N/A')}",
        "",
        "## Exception details",
    ]
    for exc in (error.get("exception") or {}).get("values") or []:
        lines.append(f"- Type: {exc.get('type', 'Unknown')}")
        lines.append(f"- Message: {exc.get('value', '')}")
        stack = (exc.get("stacktrace") or {}).get("frames") or []
        if stack:
            frames = list(reversed(stack[-25:]))
            in_app_frames = [f for f in frames if f.get("in_app") is True]
            if in_app_frames:
                frames = in_app_frames + [f for f in frames if f.get("in_app") is not True][:25]
            else:
                frames = frames[:25]
            lines.append("")
            lines.append("### Stack trace (newest first)")
            for i, frame in enumerate(frames, 1):
                lines.extend(_format_frame(frame, i))
                lines.append("")
    metadata = error.get("metadata") or {}
    if metadata:
        lines.append("")
        lines.append("## Metadata")
        lines.append(json.dumps(metadata, indent=2))
    return "\n".join(lines).rstrip()


def _format_issue_prompt(payload: dict[str, Any]) -> str:
    issue = payload.get("data", {}).get("issue", {})
    metadata = issue.get("metadata") or {}
    lines = [
        "Fix the following issue reported by Sentry (escalated, regressed, or unhandled).",
        "",
        f"**Title**: {issue.get('title', 'Unknown')}",
        f"**Level**: {issue.get('level', 'error')}",
        f"**Platform**: {issue.get('platform', 'unknown')}",
        f"**Culprit**: {issue.get('culprit', 'N/A')}",
        f"**Sentry URL**: {issue.get('web_url') or issue.get('permalink', 'N/A')}",
    ]
    for k, v in [("count", issue.get("count")), ("lastSeen", issue.get("lastSeen")), ("firstSeen", issue.get("firstSeen"))]:
        if v is not None:
            lines.append(f"**{k}**: {v}")
    lines.extend(["", "## Location (from metadata)"])
    filename = metadata.get("filename")
    func = metadata.get("function")
    if filename or func:
        if filename:
            lines.append(f"- **File**: `{filename}`")
        if func:
            lines.append(f"- **Function**: `{func}`")
        if metadata.get("type"):
            lines.append(f"- **Exception type**: {metadata['type']}")
        if metadata.get("value"):
            lines.append(f"- **Message**: {metadata['value']}")
        lines.append("")
    lines.append("## Full metadata")
    lines.append(json.dumps(metadata, indent=2))
    return "\n".join(lines)


def _get_slug(entity: Any) -> str | None:
    if isinstance(entity, dict):
        return entity.get("slug")
    return str(entity) if entity else None


def handle_sentry_webhook(payload: dict[str, Any], resource: str) -> dict[str, Any]:
    data = payload.get("data", {})
    action = payload.get("action", "")
    if resource == "error":
        if action != "created":
            return {"status": "ignored", "reason": f"action '{action}' not relevant"}
        if not _is_unhandled_error(payload):
            return {"status": "ignored", "reason": "handled exception"}
        prompt = _format_error_prompt(payload)
        slug = _get_slug(data.get("error", {}).get("project"))
    elif resource == "issue":
        if action not in ("created", "unresolved"):
            return {"status": "ignored", "reason": f"action '{action}' not relevant"}
        if not _is_unhandled_issue(payload):
            return {"status": "ignored", "reason": "handled issue"}
        if action == "unresolved":
            substatus = data.get("issue", {}).get("substatus")
            if substatus not in ("escalating", "regressed"):
                return {"status": "ignored", "reason": "issue unresolved but not escalating or regressed"}
        prompt = _format_issue_prompt(payload)
        slug = _get_slug(data.get("issue", {}).get("project"))
    else:
        return {"status": "ignored", "reason": f"unknown resource '{resource}'"}

    repo = _resolve_repository(slug)
    if not repo:
        return {"status": "error", "reason": "no repository configured for this project"}

    config = get_config()
    source = {"repository": repo, "ref": config["github_ref"]}
    target: dict[str, Any] = {}
    if config["cursor_auto_create_pr"]:
        target["autoCreatePr"] = True
        event_id = data.get("error", {}).get("event_id") or data.get("issue", {}).get("id") or ""
        target["branchName"] = f"cursor/fix-sentry-{str(event_id).replace('-', '')[:8] or 'fix'}"

    result = launch_agent(prompt, source, target or None)
    if result:
        agent_url = result.get("target", {}).get("url", "")
        entity = data.get("error") or data.get("issue") or {}
        sentry_url = entity.get("web_url") or entity.get("permalink", "")
        title = entity.get("title", "Unknown")
        record_run(sentry_url, agent_url, title)
        return {"status": "triggered", "agent_id": result.get("id"), "agent_url": agent_url}
    return {"status": "error", "reason": "failed to launch Cursor agent"}

import logging
from typing import Any

import httpx

from app.config import get_config

logger = logging.getLogger(__name__)

CURSOR_AGENTS_URL = "https://api.cursor.com/v0/agents"


def launch_agent(prompt: str, source: dict[str, str], target: dict[str, Any] | None = None) -> dict[str, Any] | None:
    config = get_config()
    if not config["cursor_api_key"]:
        logger.error("CURSOR_API_KEY not set")
        return None
    if not source.get("repository"):
        logger.error("GITHUB_REPOSITORY not set and no project mapping")
        return None

    payload: dict[str, Any] = {
        "prompt": {"text": prompt},
        "model": config["cursor_model"],
        "source": source,
    }
    if target:
        payload["target"] = target

    auth = (config["cursor_api_key"], "")
    try:
        resp = httpx.post(CURSOR_AGENTS_URL, json=payload, auth=auth, timeout=60.0)
        resp.raise_for_status()
        data = resp.json()
        logger.info("Launched Cursor agent: %s", data.get("id"))
        return data
    except httpx.HTTPStatusError as e:
        logger.error("Cursor API error: %s %s", e.response.status_code, e.response.text)
        return None
    except Exception as e:
        logger.exception("Failed to launch Cursor agent: %s", e)
        return None

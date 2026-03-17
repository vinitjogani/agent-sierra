import json
import logging
import time
import uuid
from functools import cache
from typing import Any

import redis

from app.config import RUN_TTL_SECONDS, get_config

logger = logging.getLogger(__name__)
RUNS_KEY = "agent_sierra:runs"


@cache
def _redis() -> redis.Redis:
    return redis.from_url(get_config()["redis_url"], decode_responses=True)


def record_run(sentry_url: str, cursor_url: str, title: str) -> None:
    try:
        r = _redis()
        created = time.time()
        payload = json.dumps({
            "id": str(uuid.uuid4()),
            "title": title[:200],
            "sentry_url": sentry_url,
            "cursor_url": cursor_url,
            "created_at": created,
        })
        r.zadd(RUNS_KEY, {payload: created})
    except Exception as e:
        logger.warning("Failed to record run to Redis: %s", e)


def get_recent_runs(limit: int = 100) -> list[dict[str, Any]]:
    try:
        r = _redis()
        cutoff = time.time() - RUN_TTL_SECONDS
        r.zremrangebyscore(RUNS_KEY, "-inf", cutoff)
        raw = r.zrevrange(RUNS_KEY, 0, limit - 1)
        runs = []
        for item in raw:
            try:
                runs.append(json.loads(item))
            except json.JSONDecodeError:
                pass
        return runs
    except Exception as e:
        logger.warning("Failed to fetch runs from Redis: %s", e)
        return []

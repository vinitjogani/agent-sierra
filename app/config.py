import os
from functools import lru_cache

from dotenv import load_dotenv

load_dotenv()


RUN_TTL_SECONDS = 7 * 24 * 60 * 60


@lru_cache
def get_config():
    return {
        "cursor_api_key": os.getenv("CURSOR_API_KEY"),
        "cursor_model": os.getenv("CURSOR_MODEL", "cursor-composer-1-5"),
        "github_repository": os.getenv("GITHUB_REPOSITORY"),
        "github_ref": os.getenv("GITHUB_REF", "main"),
        "cursor_auto_create_pr": os.getenv("CURSOR_AUTO_CREATE_PR", "true").lower() == "true",
        "redis_url": os.getenv("REDIS_URL", "redis://localhost:6379/0"),
        "sentry_webhook_secret": os.getenv("SENTRY_WEBHOOK_SECRET", "").strip() or None,
        "sentry_auth_token": os.getenv("SENTRY_AUTH_TOKEN", "").strip() or None,
        "dashboard_password": os.getenv("DASHBOARD_PASSWORD", "").strip() or None,
        "project_mapping": _parse_project_mapping(),
    }


def _parse_project_mapping() -> dict[str, str]:
    return {
        k.replace("SENTRY_PROJECT_", "").lower(): v
        for k, v in os.environ.items()
        if k.startswith("SENTRY_PROJECT_") and k != "SENTRY_PROJECT_SLUG"
    }

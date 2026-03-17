import base64
import hashlib
import hmac
import logging
import secrets
from typing import Annotated

from fastapi import Depends, HTTPException, Request, Response, status
from fastapi.security import HTTPBasic, HTTPBasicCredentials

logger = logging.getLogger(__name__)

SESSION_COOKIE = "agent_sierra_session"
SESSION_MAX_AGE = 60 * 60 * 24 * 7  # 7 days


def _verify_sentry_signature(
    secret: str | None, body: bytes, signature: str | None
) -> bool:
    if not secret or not signature:
        return False
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


def verify_sentry_webhook(request: Request, body: bytes) -> None:
    import time

    from app.config import get_config

    secret = get_config().get("sentry_webhook_secret")
    if not secret:
        raise HTTPException(
            status_code=503, detail="SENTRY_WEBHOOK_SECRET not configured"
        )
    sig = request.headers.get("Sentry-Hook-Signature", "").strip()
    if not _verify_sentry_signature(secret, body, sig):
        logger.warning("Sentry webhook signature verification failed")
        raise HTTPException(status_code=401, detail="Invalid webhook signature")
    ts_header = request.headers.get("Sentry-Hook-Timestamp", "").strip()
    if ts_header:
        try:
            ts = int(ts_header)
            if abs(time.time() - ts) > 300:
                logger.warning("Sentry webhook timestamp too old or in future")
                raise HTTPException(status_code=401, detail="Webhook timestamp expired")
        except ValueError:
            pass


def _make_token(password: str) -> str:
    return base64.urlsafe_b64encode(
        hashlib.sha256(f"{password}:agent_sierra".encode()).digest()
    ).decode()


def _is_browser_request(request: Request) -> bool:
    accept = request.headers.get("accept", "")
    return "text/html" in accept


async def require_dashboard_auth(
    request: Request,
    credentials: Annotated[
        HTTPBasicCredentials | None, Depends(HTTPBasic(auto_error=False))
    ],
) -> None:
    from app.config import get_config

    password = get_config().get("dashboard_password")
    if not password:
        raise HTTPException(status_code=503, detail="DASHBOARD_PASSWORD not configured")
    valid_token = _make_token(password)
    session = request.cookies.get(SESSION_COOKIE)
    if session and secrets.compare_digest(session, valid_token):
        return
    if credentials and secrets.compare_digest(credentials.password, password):
        return
    if _is_browser_request(request):
        raise HTTPException(
            status_code=status.HTTP_302_FOUND,
            detail="Login required",
            headers={"Location": "/login"},
        )
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid credentials",
        headers={"WWW-Authenticate": 'Basic realm="Sierra"'},
    )


def set_session_cookie(response: Response, password: str) -> None:
    token = _make_token(password)
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=SESSION_MAX_AGE,
        httponly=True,
        samesite="lax",
        path="/",
    )


def clear_session(response: Response) -> None:
    response.delete_cookie(SESSION_COOKIE, path="/")


RequireAuth = Depends(require_dashboard_auth)

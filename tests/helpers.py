"""Helpers for tests that need a signed-in browser session."""

import base64
import json

import itsdangerous
from httpx2 import AsyncClient

from app.config import get_settings
from app.main import SESSION_COOKIE
from app.models import User
from app.web.csrf import CSRF_HEADER, CSRF_SESSION_KEY
from app.web.deps import SESSION_USER_KEY

CSRF_TOKEN = "test-csrf-token"
HTMX = {"HX-Request": "true"}


def session_cookie(data: dict) -> str:
    """A session cookie exactly as Starlette's SessionMiddleware would issue it."""
    signer = itsdangerous.TimestampSigner(get_settings().secret_key.get_secret_value())
    return signer.sign(base64.b64encode(json.dumps(data).encode())).decode()


def sign_in(client: AsyncClient, user: User | None) -> dict[str, str]:
    """Give `client` a signed-in session. Returns headers that pass the CSRF check.

    Pass `user=None` for a session that has a CSRF token but nobody signed in.
    """
    data = {CSRF_SESSION_KEY: CSRF_TOKEN}
    if user is not None:
        data[SESSION_USER_KEY] = str(user.id)
    # Same domain the cookie jar files server cookies under, so that a later
    # Set-Cookie from the app (for example on sign-out) replaces this one.
    client.cookies.delete(SESSION_COOKIE) if SESSION_COOKIE in client.cookies else None
    client.cookies.set(SESSION_COOKIE, session_cookie(data), domain="testserver.local")
    return {CSRF_HEADER: CSRF_TOKEN}

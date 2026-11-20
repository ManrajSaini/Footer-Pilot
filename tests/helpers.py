"""Helpers for tests that need a signed-in browser session."""

import base64
import html as html_module
import json
import re

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


# --- The designer form ---------------------------------------------------------------

# What the designer form posts for an untouched default design.
BASE_FORM = {
    "template": "classic",
    "primary_color": "#1d4ed8",
    "text_color": "#1f2937",
    "font_family": "Arial",
    "font_size": "medium",
    "logo_asset_id": "",
    "logo_width": "120",
    "logo_link_url": "",
    "show_photo": "1",
    "show_job_title": "1",
    "show_department": "1",
    "show_phone_work": "1",
    "show_phone_mobile": "1",
    "show_email": "1",
    "company_name": "Acme",
    "company_tagline": "",
    "company_address": "",
    "company_phone": "",
    "company_website_url": "https://acme.test",
    "social_linkedin": "",
    "social_x": "",
    "social_facebook": "",
    "social_instagram": "",
    "social_youtube": "",
    "social_github": "",
}


def form(**changes) -> dict[str, str]:
    """BASE_FORM with changes. Pass `name=None` to leave a field (a checkbox) out."""
    data = {**BASE_FORM, **{k: v for k, v in changes.items() if v is not None}}
    for name, value in changes.items():
        if value is None:
            data.pop(name, None)
    return data


def preview_of(response_text: str) -> str:
    """The signature HTML inside the preview frame of a response."""
    match = re.search(r'srcdoc="([^"]*)"', response_text)
    assert match, "no preview frame in response"
    document = html_module.unescape(match.group(1))
    return document.split("<body>", 1)[1].rsplit("</body>", 1)[0]

"""CSRF protection for state-changing requests.

The token lives in the signed session cookie. Pages that submit forms put it in a
`<meta name="csrf-token">` tag and send it back either as the `X-CSRF-Token` header
(HTMX, via `hx-headers`) or as a hidden `csrf_token` form field.

`csrf_protect` is registered as an application-wide dependency, so every route is
covered unless its path is listed in `CSRF_EXEMPT_PATHS`.
"""

import secrets

from fastapi import HTTPException, Request, status

CSRF_SESSION_KEY = "csrf_token"
CSRF_HEADER = "X-CSRF-Token"
CSRF_FORM_FIELD = "csrf_token"

SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "TRACE"})

# Endpoints called by other servers, which authenticate by request signature instead.
CSRF_EXEMPT_PATHS: frozenset[str] = frozenset()

_FORM_CONTENT_TYPES = ("application/x-www-form-urlencoded", "multipart/form-data")


def get_csrf_token(request: Request) -> str:
    """Return the session's CSRF token, creating it on first use."""
    token = request.session.get(CSRF_SESSION_KEY)
    if not token:
        token = secrets.token_urlsafe(32)
        request.session[CSRF_SESSION_KEY] = token
    return token


async def csrf_protect(request: Request) -> None:
    if request.method in SAFE_METHODS or request.url.path in CSRF_EXEMPT_PATHS:
        return

    expected = request.session.get(CSRF_SESSION_KEY)
    supplied = request.headers.get(CSRF_HEADER)
    if supplied is None and request.headers.get("content-type", "").startswith(_FORM_CONTENT_TYPES):
        value = (await request.form()).get(CSRF_FORM_FIELD)
        supplied = value if isinstance(value, str) else None

    if not expected or not supplied or not secrets.compare_digest(expected, supplied):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "CSRF token missing or invalid")

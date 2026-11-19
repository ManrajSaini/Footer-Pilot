"""Sign-in with Google (OpenID Connect) and sign-out.

Only `openid email profile` is requested here. Access to a customer's directory and
signatures is separate and comes from domain-wide delegation (see app/google).
"""

import logging
from typing import Any

import httpx2
from authlib.integrations.base_client.errors import OAuthError
from authlib.integrations.starlette_client import OAuth
from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.db import get_session
from app.models import User
from app.services import accounts
from app.services.accounts import GoogleIdentity
from app.web.deps import APP_HOME_PATH, current_user, start_session
from app.web.templating import templates

router = APIRouter()
logger = logging.getLogger(__name__)

CALLBACK_PATH = "/auth/google/callback"


def build_google_oauth(settings: Settings) -> Any | None:
    """The Authlib client for Google, or None when sign-in is not configured.

    Endpoints are listed explicitly instead of being discovered, so starting a sign-in
    needs no network call to Google.
    """
    if not settings.google_sign_in_configured:
        return None
    oauth = OAuth()
    return oauth.register(
        name="google",
        client_id=settings.google_oauth_client_id,
        client_secret=settings.google_oauth_client_secret.get_secret_value(),
        authorize_url="https://accounts.google.com/o/oauth2/v2/auth",
        access_token_url="https://oauth2.googleapis.com/token",  # noqa: S106 - a URL
        jwks_uri="https://www.googleapis.com/oauth2/v3/certs",
        issuer="https://accounts.google.com",
        client_kwargs={"scope": "openid email profile", "timeout": 20},
    )


class WorkspaceAccountRequired(Exception):
    """The Google account is a personal one, not part of a Workspace domain."""


def identity_from_claims(claims: dict[str, Any]) -> GoogleIdentity:
    sub = claims.get("sub")
    email = claims.get("email")
    if not sub or not email or claims.get("email_verified") is not True:
        raise ValueError("Google did not return a verified email address")
    # `hd` is only present for Google Workspace accounts.
    domain = claims.get("hd")
    if not domain:
        raise WorkspaceAccountRequired
    return GoogleIdentity(
        sub=str(sub),
        email=str(email),
        domain=str(domain),
        name=str(claims.get("name") or ""),
        picture_url=claims.get("picture") or None,
    )


def _sign_in_page(request: Request, *, error: str | None = None, status_code: int = 200):
    settings = get_settings()
    return templates.TemplateResponse(
        request,
        "auth/sign_in.html",
        {
            "error": error,
            "google_configured": settings.google_sign_in_configured,
            "dev_login": settings.dev_login,
        },
        status_code=status_code,
    )


@router.get("/signup", response_class=HTMLResponse)
@router.get("/login", response_class=HTMLResponse)
async def sign_in_page(request: Request, user: User | None = Depends(current_user)) -> Response:
    if user is not None:
        return RedirectResponse(APP_HOME_PATH, status_code=303)
    return _sign_in_page(request)


@router.get("/auth/google/login")
async def google_login(request: Request) -> Response:
    client = request.app.state.oauth_google
    if client is None:
        return _sign_in_page(
            request, error="Google sign-in is not configured on this server.", status_code=503
        )
    redirect_uri = f"{get_settings().app_base_url}{CALLBACK_PATH}"
    # hd=* asks Google to offer only Workspace accounts; the callback still checks it.
    return await client.authorize_redirect(request, redirect_uri, prompt="select_account", hd="*")


@router.get(CALLBACK_PATH)
async def google_callback(
    request: Request, session: AsyncSession = Depends(get_session)
) -> Response:
    client = request.app.state.oauth_google
    if client is None:
        return RedirectResponse("/signup", status_code=303)
    try:
        token = await client.authorize_access_token(request)
        identity = identity_from_claims(token.get("userinfo") or {})
    except WorkspaceAccountRequired:
        return templates.TemplateResponse(request, "auth/workspace_required.html", status_code=403)
    except OAuthError as error:
        # The user cancelled, or the sign-in was started in another browser session.
        logger.info("google sign-in not completed error=%s", error.error)
        return _sign_in_page(
            request, error="Sign-in was not completed. Please try again.", status_code=400
        )
    except (httpx2.HTTPError, ValueError) as error:
        logger.warning("google sign-in failed error=%s", type(error).__name__)
        return _sign_in_page(
            request,
            error="We could not complete the sign-in with Google. Please try again.",
            status_code=502,
        )

    user = await accounts.sign_in(session, identity)
    start_session(request, user)
    return RedirectResponse(APP_HOME_PATH, status_code=303)


@router.post("/auth/logout")
async def logout(request: Request) -> Response:
    request.session.clear()
    return RedirectResponse("/", status_code=303)

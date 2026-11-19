"""Who is signed in, and what they may reach.

    require_user           signed in
    require_member         signed in, and allowed into their organisation
    require_connected_org  as above, and the domain is connected (alias: current_org)

Handlers never take an organisation from a URL or form field: it always comes from the
session through these dependencies, which is what keeps tenants apart.
"""

import uuid

from fastapi import Depends, FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.models import Organization, User
from app.services import accounts
from app.web.templating import templates

SESSION_USER_KEY = "user_id"
SIGN_IN_PATH = "/signup"
SETUP_PATH = "/app/setup"
APP_HOME_PATH = "/app"


class LoginRequired(Exception):
    pass


class SetupRequired(Exception):
    pass


class AccessDenied(Exception):
    """The organisation already has an owner, and it is not this user."""

    def __init__(self, user: User, owner_email: str | None) -> None:
        self.user = user
        self.owner_email = owner_email


def start_session(request: Request, user: User) -> None:
    request.session.clear()  # never carry a pre-login session over
    request.session[SESSION_USER_KEY] = str(user.id)


async def current_user(
    request: Request, session: AsyncSession = Depends(get_session)
) -> User | None:
    raw = request.session.get(SESSION_USER_KEY)
    if not raw:
        return None
    try:
        user = await session.get(User, uuid.UUID(raw))
    except ValueError:
        user = None
    if user is None:
        request.session.clear()
    return user


async def require_user(user: User | None = Depends(current_user)) -> User:
    if user is None:
        raise LoginRequired
    return user


async def require_member(
    user: User = Depends(require_user), session: AsyncSession = Depends(get_session)
) -> User:
    organization = user.organization
    if organization.owner_user_id is not None and not user.owns(organization):
        raise AccessDenied(user, await accounts.owner_email(session, organization))
    return user


async def require_connected_org(user: User = Depends(require_member)) -> Organization:
    if not user.organization.is_connected:
        raise SetupRequired
    return user.organization


current_org = require_connected_org


def _redirect(request: Request, path: str) -> Response:
    if request.headers.get("HX-Request"):
        # HTMX follows this header with a full page load instead of swapping a fragment.
        return Response(status_code=401, headers={"HX-Redirect": path})
    return RedirectResponse(path, status_code=303)


async def _login_required(request: Request, exc: LoginRequired) -> Response:
    return _redirect(request, SIGN_IN_PATH)


async def _setup_required(request: Request, exc: SetupRequired) -> Response:
    return _redirect(request, SETUP_PATH)


async def _access_denied(request: Request, exc: AccessDenied) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "app/already_setup.html",
        {"user": exc.user, "owner_email": exc.owner_email},
        status_code=403,
    )


def register_access_handlers(app: FastAPI) -> None:
    app.add_exception_handler(LoginRequired, _login_required)  # type: ignore[arg-type]
    app.add_exception_handler(SetupRequired, _setup_required)  # type: ignore[arg-type]
    app.add_exception_handler(AccessDenied, _access_denied)  # type: ignore[arg-type]

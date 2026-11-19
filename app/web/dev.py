"""Development-only sign-in, without Google.

This router is only registered when DEV_LOGIN=true, and Settings refuses that value
when ENV=production. With GOOGLE_MODE=fake the address you sign in with decides how
the fake Google behaves (see app/google/fake.py).
"""

import re

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.services import accounts
from app.services.accounts import GoogleIdentity
from app.web.deps import APP_HOME_PATH, start_session
from app.web.templating import templates

router = APIRouter()

_EMAIL = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")

PERSONAS = [
    ("admin@acme.test", "Super admin, everything works"),
    ("nodelegation@acme.test", "Delegation not added yet"),
    ("nogmail@acme.test", "Delegation is missing the Gmail scope"),
    ("notadmin@acme.test", "Signed-in user is not an admin"),
    ("outage@acme.test", "Google is unreachable"),
]


def _page(request: Request, *, error: str | None = None, status_code: int = 200) -> HTMLResponse:
    return templates.TemplateResponse(
        request, "dev/login.html", {"personas": PERSONAS, "error": error}, status_code=status_code
    )


@router.get("/dev/login", response_class=HTMLResponse)
async def dev_login_page(request: Request) -> HTMLResponse:
    return _page(request)


@router.post("/dev/login")
async def dev_login(
    request: Request,
    email: str = Form(),
    session: AsyncSession = Depends(get_session),
) -> Response:
    email = email.strip().lower()
    if not _EMAIL.fullmatch(email):
        return _page(request, error="Enter an email address.", status_code=400)
    local, _, domain = email.partition("@")
    user = await accounts.sign_in(
        session,
        GoogleIdentity(sub=f"dev:{email}", email=email, domain=domain, name=local.title()),
    )
    start_session(request, user)
    return RedirectResponse(APP_HOME_PATH, status_code=303)

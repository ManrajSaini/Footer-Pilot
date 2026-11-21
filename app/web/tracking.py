"""The public address behind every counted link in a signature.

    GET /c/<token>?e=<employee short id>

Records the click and sends the visitor on. Anyone on the internet can call this, so
it does as little as possible: no session, no cookies, and the destination comes only
from the stored link, never from the request.
"""

import re

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.services import links as link_service
from app.services.links import TOKEN_LENGTH

router = APIRouter()

_TOKEN = re.compile(rf"[A-Za-z0-9]{{{TOKEN_LENGTH}}}")
_SHORT_ID = re.compile(r"[A-Za-z0-9_-]{1,16}")

_HEADERS = {
    # Each click must reach us: a cached redirect would not be counted.
    "Cache-Control": "no-store",
    "X-Robots-Tag": "noindex, nofollow",
    # The destination has no need to learn the tracking address.
    "Referrer-Policy": "no-referrer",
}


@router.api_route("/c/{token}", methods=["GET", "HEAD"], include_in_schema=False)
async def click(
    request: Request,
    token: str,
    e: str = "",
    session: AsyncSession = Depends(get_session),
) -> RedirectResponse:
    if not _TOKEN.fullmatch(token):
        raise HTTPException(status_code=404)

    if request.method == "HEAD":
        # Link checkers ask with HEAD. They get the same answer; nothing is recorded.
        target = await link_service.destination(session, token)
    else:
        target = await link_service.record_click(
            session,
            token,
            e if _SHORT_ID.fullmatch(e) else None,
            request.headers.get("user-agent"),
        )
    if target is None:
        raise HTTPException(status_code=404)
    return RedirectResponse(target, status_code=302, headers=_HEADERS)

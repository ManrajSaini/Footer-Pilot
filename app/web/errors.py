"""HTML error pages."""

import logging

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.web.templating import templates

logger = logging.getLogger(__name__)

_MESSAGES = {
    403: ("Not allowed", "You do not have access to this page, or your session expired."),
    404: ("Page not found", "The page you are looking for does not exist or has moved."),
    500: ("Something went wrong", "An unexpected error occurred. Please try again shortly."),
}
_DEFAULT_MESSAGE = ("Request failed", "The request could not be completed.")


def _error_page(request: Request, status_code: int, headers=None) -> HTMLResponse:
    title, message = _MESSAGES.get(status_code, _DEFAULT_MESSAGE)
    return templates.TemplateResponse(
        request,
        "errors/error.html",
        {"status_code": status_code, "title": title, "message": message},
        status_code=status_code,
        headers=headers,
    )


async def _http_exception(request: Request, exc: StarletteHTTPException) -> HTMLResponse:
    return _error_page(request, exc.status_code, exc.headers)


async def _unhandled_exception(request: Request, exc: Exception) -> HTMLResponse:
    logger.exception("unhandled error method=%s path=%s", request.method, request.url.path)
    return _error_page(request, 500)


def register_error_handlers(app: FastAPI) -> None:
    app.add_exception_handler(StarletteHTTPException, _http_exception)  # type: ignore[arg-type]
    app.add_exception_handler(Exception, _unhandled_exception)

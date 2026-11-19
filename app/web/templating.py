"""Jinja environment for pages and HTMX partials (not for signature rendering)."""

from pathlib import Path
from typing import Any

from fastapi import Request
from fastapi.templating import Jinja2Templates

from app.web.csrf import get_csrf_token

APP_DIR = Path(__file__).resolve().parent.parent
TEMPLATES_DIR = APP_DIR / "templates"
STATIC_DIR = APP_DIR / "static"


def _template_globals(request: Request) -> dict[str, Any]:
    # A callable, so the session cookie is only set on pages that actually use it.
    return {"csrf_token": lambda: get_csrf_token(request)}


templates = Jinja2Templates(directory=TEMPLATES_DIR, context_processors=[_template_globals])

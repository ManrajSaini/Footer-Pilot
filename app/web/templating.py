"""Jinja environment for pages and HTMX partials (not for signature rendering)."""

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import Request
from fastapi.templating import Jinja2Templates

from app.config import get_settings
from app.web.csrf import get_csrf_token

APP_DIR = Path(__file__).resolve().parent.parent
TEMPLATES_DIR = APP_DIR / "templates"
STATIC_DIR = APP_DIR / "static"


def _template_globals(request: Request) -> dict[str, Any]:
    # A callable, so the session cookie is only set on pages that actually use it.
    return {
        "csrf_token": lambda: get_csrf_token(request),
        "marketing_base_url": get_settings().marketing_base_url,
    }


def timeago(value: datetime | None, now: datetime | None = None) -> str:
    """A past moment in words: "just now", "5 minutes ago", "3 hours ago", then a date."""
    if value is None:
        return "never"
    seconds = ((now or datetime.now(UTC)) - value).total_seconds()
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        minutes = int(seconds // 60)
        return f"{minutes} minute{'' if minutes == 1 else 's'} ago"
    if seconds < 86400:
        hours = int(seconds // 3600)
        return f"{hours} hour{'' if hours == 1 else 's'} ago"
    return value.strftime("on %d %b %Y")


templates = Jinja2Templates(directory=TEMPLATES_DIR, context_processors=[_template_globals])
templates.env.filters["timeago"] = timeago

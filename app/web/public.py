"""Public marketing pages."""

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from app.web.templating import templates

router = APIRouter()


@router.get("/", response_class=HTMLResponse)
async def home(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request, "public/home.html")

"""Public marketing pages and crawler files."""

from xml.sax.saxutils import escape

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, Response

from app.config import get_settings
from app.web.templating import templates

router = APIRouter()

# Paths listed in the sitemap. Add new public, indexable pages here.
SITEMAP_PATHS = ("/", "/email-signature-generator")


@router.get("/", response_class=HTMLResponse)
async def home(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request, "public/home.html")


@router.get("/signup", response_class=HTMLResponse)
async def signup(request: Request) -> HTMLResponse:
    # Placeholder: Phase 2 replaces this with Google sign-in.
    return templates.TemplateResponse(request, "public/signup.html")


@router.get("/robots.txt", include_in_schema=False)
async def robots() -> PlainTextResponse:
    base = get_settings().marketing_base_url
    lines = ["User-agent: *", "Allow: /", "Disallow: /generator/", f"Sitemap: {base}/sitemap.xml"]
    return PlainTextResponse("\n".join(lines) + "\n")


@router.get("/sitemap.xml", include_in_schema=False)
async def sitemap() -> Response:
    base = get_settings().marketing_base_url
    urls = "".join(f"<url><loc>{escape(base + path)}</loc></url>" for path in SITEMAP_PATHS)
    body = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{urls}</urlset>'
    )
    return Response(body, media_type="application/xml")

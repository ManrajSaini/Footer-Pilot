import html as html_module

import pytest
from fastapi import FastAPI
from httpx import AsyncClient

from app.web.generator import PREVIEW_PATH
from app.web.ratelimit import RateLimiter

VALID_FORM = {
    "full_name": "Jordan Lee",
    "job_title": "Account Executive",
    "company_name": "Northwind",
    "phone": "+44 20 7946 0000",
    "email": "jordan@northwind.example",
    "website_url": "northwind.example",
    "logo_url": "",
    "logo_width": "120",
    "photo_url": "",
    "template": "classic",
    "primary_color": "#0f766e",
    "text_color": "#111827",
    "font_family": "Georgia",
    "font_size": "large",
    "social_linkedin": "https://www.linkedin.com/in/jordanlee",
    "social_x": "",
    "social_facebook": "",
    "social_instagram": "",
    "social_youtube": "",
    "social_github": "",
}


def signature_from(response_text: str) -> str:
    """The signature HTML carried in the response's hidden textarea."""
    start = response_text.index("<textarea")
    start = response_text.index(">", start) + 1
    return html_module.unescape(response_text[start : response_text.index("</textarea>")])


async def test_generator_page_renders_form_and_sample_preview(client: AsyncClient):
    response = await client.get("/email-signature-generator")
    assert response.status_code == 200
    assert "Free email signature generator" in response.text
    assert 'name="full_name"' in response.text
    assert 'hx-post="/generator/preview"' in response.text
    assert "srcdoc=" in response.text
    assert "Alex Morgan" in signature_from(response.text)
    assert "Need this for your whole team?" in response.text
    assert (
        '<link rel="canonical" href="http://testserver/email-signature-generator">' in response.text
    )
    # Public and stateless: no session cookie.
    assert "set-cookie" not in response.headers


async def test_preview_renders_submitted_details(client: AsyncClient):
    response = await client.post(PREVIEW_PATH, data=VALID_FORM)
    assert response.status_code == 200
    assert "HX-Reswap" not in response.headers
    assert "srcdoc=" in response.text

    signature = signature_from(response.text)
    assert "Jordan Lee" in signature
    assert "Account Executive" in signature
    assert "Northwind" in signature
    assert 'href="mailto:jordan@northwind.example"' in signature
    assert 'href="https://northwind.example"' in signature  # scheme added
    assert "color:#0f766e" in signature
    assert "font-family:Georgia" in signature
    assert "font-size:14px" in signature
    # Social icons are absolute URLs on the asset host.
    assert 'src="http://testserver/static/social/linkedin.png"' in signature
    # A successful preview clears any earlier errors.
    assert '<div id="generator-errors" hx-swap-oob="true">' in response.text
    assert 'role="alert"' not in response.text


async def test_preview_needs_no_csrf_token_and_sets_no_cookie(client: AsyncClient):
    response = await client.post(PREVIEW_PATH, data=VALID_FORM)
    assert response.status_code == 200
    assert "set-cookie" not in response.headers


@pytest.mark.parametrize("template", ["classic", "side-logo", "photo", "minimal"])
async def test_preview_supports_every_layout(client: AsyncClient, template: str):
    form = {
        **VALID_FORM,
        "template": template,
        "logo_url": "https://cdn.example/logo.png",
        "photo_url": "https://cdn.example/me.jpg",
    }
    response = await client.post(PREVIEW_PATH, data=form)
    assert response.status_code == 200
    assert "Jordan Lee" in signature_from(response.text)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("website_url", "javascript:alert(1)", "Website: Web addresses must start with"),
        ("logo_url", "data:image/png;base64,AAAA", "Logo image URL: Web addresses must start with"),
        ("social_x", "ftp://x.com/me", "X: Web addresses must start with"),
        ("email", "not-an-email", "Email: Enter a valid email address"),
        ("primary_color", "blue", "Accent colour: Enter a colour"),
        ("full_name", "x" * 81, "Full name: Use at most 80 characters"),
        ("template", "fancy", "Layout: This value is not valid"),
        ("logo_width", "9000", "Logo width: This value is not valid"),
    ],
)
async def test_invalid_input_returns_errors_and_keeps_the_preview(
    client: AsyncClient, field: str, value: str, message: str
):
    response = await client.post(PREVIEW_PATH, data={**VALID_FORM, field: value})
    assert response.status_code == 200
    # The page keeps the last good preview; only the error list is swapped in.
    assert response.headers["HX-Reswap"] == "none"
    assert "srcdoc=" not in response.text
    assert 'role="alert"' in response.text
    assert message in html_module.unescape(response.text)


async def test_oversized_signature_is_reported(client: AsyncClient):
    long_url = "https://example.com/" + "a" * 1900
    form = {**VALID_FORM, **{f"social_{n}": long_url for n in ("linkedin", "x", "facebook")}}
    form.update({f"social_{n}": long_url for n in ("instagram", "youtube", "github")})
    response = await client.post(PREVIEW_PATH, data=form)
    assert response.headers["HX-Reswap"] == "none"
    assert "Gmail allows 10,000" in response.text


async def test_html_in_input_cannot_escape_into_the_page(client: AsyncClient):
    attack = '<img src=x onerror=alert(1)>"</textarea><script>alert(2)</script>'
    response = await client.post(PREVIEW_PATH, data={**VALID_FORM, "full_name": attack})
    assert response.status_code == 200
    assert "<script" not in response.text
    assert "<img src=x" not in response.text
    assert response.text.count("</textarea>") == 1
    # Inside the signature itself the text is escaped too.
    signature = signature_from(response.text)
    assert "<script" not in signature
    assert "&lt;img src=x" in signature


async def test_preview_is_rate_limited_per_client(app: FastAPI, client: AsyncClient):
    app.state.generator_limiter = RateLimiter(limit=2, window_seconds=60)
    assert (await client.post(PREVIEW_PATH, data=VALID_FORM)).status_code == 200
    assert (await client.post(PREVIEW_PATH, data=VALID_FORM)).status_code == 200
    blocked = await client.post(PREVIEW_PATH, data=VALID_FORM)
    assert blocked.status_code == 429
    assert 0 < int(blocked.headers["Retry-After"]) <= 60


@pytest.mark.parametrize("network", ["linkedin", "x", "facebook", "instagram", "youtube", "github"])
async def test_social_icons_are_served(client: AsyncClient, network: str):
    response = await client.get(f"/static/social/{network}.png")
    assert response.status_code == 200
    assert response.headers["content-type"] == "image/png"

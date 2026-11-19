from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.config import get_settings
from app.main import create_app


async def test_home_page(client: AsyncClient):
    response = await client.get("/")
    assert response.status_code == 200
    assert "One email signature for your whole company" in response.text
    # Public pages must stay cacheable: no session cookie unless a page needs one.
    assert "set-cookie" not in response.headers


async def test_vendored_scripts_are_served(client: AsyncClient):
    for name in ("htmx-2.0.11.min.js", "alpine-3.17.5.min.js"):
        response = await client.get(f"/static/vendor/{name}")
        assert response.status_code == 200, name


async def test_unknown_page_renders_404(client: AsyncClient):
    response = await client.get("/no-such-page")
    assert response.status_code == 404
    assert "Page not found" in response.text
    assert response.headers["content-type"].startswith("text/html")


async def test_unhandled_error_renders_500(app: FastAPI, client: AsyncClient):
    @app.get("/_test/boom")
    async def boom():
        raise RuntimeError("secret detail")

    response = await client.get("/_test/boom")
    assert response.status_code == 500
    assert "Something went wrong" in response.text
    assert "secret detail" not in response.text


async def test_healthz_ok(require_db, client: AsyncClient):
    response = await client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


async def test_healthz_reports_database_outage(job_queue, monkeypatch):
    # Port 1 is never a Postgres server, so the connection is refused immediately.
    monkeypatch.setenv("DATABASE_URL", "postgresql://nobody:nothing@127.0.0.1:1/none")
    get_settings.cache_clear()
    try:
        app = create_app()
        async with (
            app.router.lifespan_context(app),
            AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as http,
        ):
            response = await http.get("/healthz")
    finally:
        get_settings.cache_clear()
    assert response.status_code == 503
    assert response.json() == {"status": "unavailable"}

import pytest
from fastapi import FastAPI, Request
from httpx2 import AsyncClient

from app.web.csrf import get_csrf_token


@pytest.fixture
def csrf_app(app: FastAPI) -> FastAPI:
    @app.get("/_test/token")
    async def token(request: Request):
        return {"token": get_csrf_token(request)}

    @app.post("/_test/submit")
    async def submit():
        return {"ok": True}

    return app


async def fetch_token(client: AsyncClient) -> str:
    """Start a session (the client keeps the cookie) and return its token."""
    return (await client.get("/_test/token")).json()["token"]


async def test_post_without_session_is_rejected(csrf_app, client: AsyncClient):
    response = await client.post("/_test/submit")
    assert response.status_code == 403


async def test_post_without_token_is_rejected(csrf_app, client: AsyncClient):
    await fetch_token(client)
    response = await client.post("/_test/submit")
    assert response.status_code == 403


async def test_post_with_wrong_token_is_rejected(csrf_app, client: AsyncClient):
    await fetch_token(client)
    response = await client.post("/_test/submit", headers={"X-CSRF-Token": "wrong"})
    assert response.status_code == 403


async def test_post_with_header_token_is_accepted(csrf_app, client: AsyncClient):
    token = await fetch_token(client)
    response = await client.post("/_test/submit", headers={"X-CSRF-Token": token})
    assert response.status_code == 200


async def test_post_with_form_field_token_is_accepted(csrf_app, client: AsyncClient):
    token = await fetch_token(client)
    response = await client.post("/_test/submit", data={"csrf_token": token})
    assert response.status_code == 200


async def test_token_is_stable_within_a_session(csrf_app, client: AsyncClient):
    assert await fetch_token(client) == await fetch_token(client)


async def test_token_from_another_session_is_rejected(csrf_app, client: AsyncClient):
    other_token = await fetch_token(client)
    client.cookies.clear()
    await fetch_token(client)
    response = await client.post("/_test/submit", headers={"X-CSRF-Token": other_token})
    assert response.status_code == 403

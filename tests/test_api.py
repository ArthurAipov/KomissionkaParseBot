import httpx
import pytest

from app.api import app
from app.config import settings
from app.db import session_dependency

pytestmark = pytest.mark.postgres


@pytest.fixture
async def client(session_factory):
    def dependency():
        with session_factory() as db:
            yield db

    app.dependency_overrides[session_dependency] = dependency
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        yield client
    app.dependency_overrides.clear()


def headers():
    return {"X-API-Key": settings().api_key.get_secret_value()}


async def test_api_requires_secret(client):
    assert (await client.get("/health")).status_code == 200
    assert (await client.get("/categories")).status_code == 401
    assert (await client.post("/notifications/claim")).status_code == 401


async def test_registration_private_chat_and_keyword_idempotency(client):
    response = await client.post(
        "/users",
        headers=headers(),
        json={
            "telegram_id": 123,
            "chat_id": 456,
        },
    )
    assert response.status_code == 422
    for _ in range(2):
        response = await client.post(
            "/users",
            headers=headers(),
            json={
                "telegram_id": 123,
                "chat_id": 123,
            },
        )
        assert response.status_code == 200
    one = await client.post("/users/123/keywords", headers=headers(), json={"name": "MacBook"})
    two = await client.post("/users/123/keywords", headers=headers(), json={"name": " macbook "})
    assert one.status_code == two.status_code == 200
    assert one.json()["id"] == two.json()["id"]
    assert not one.json()["ready"]
    response = await client.get("/users/123/subscriptions", headers=headers())
    assert len(response.json()["subscriptions"]) == 1


async def test_parse_query_returns_shared_job_and_rejects_external_urls(client):
    one = await client.get("/parse/search", headers=headers(), params={"query": "macbook"})
    two = await client.get("/parse/search", headers=headers(), params={"query": "MACBOOK"})
    assert one.status_code == two.status_code == 202
    assert one.json()["id"] == two.json()["id"]
    assert one.headers["Location"].startswith("/parse/jobs/")
    response = await client.get(
        "/parse/product",
        headers=headers(),
        params={
            "url": "https://evil.test/catalog/",
        },
    )
    assert response.status_code == 422


async def test_saved_preview_and_media_cache_require_auth(client):
    assert (await client.post("/users/123/preview")).status_code == 401
    assert (await client.post("/media/cache", json={"photos": []})).status_code == 401


async def test_photo_references_are_idempotent_and_invalidatable(client, monkeypatch):
    from pydantic import SecretStr

    monkeypatch.setattr(settings(), "bot_token", SecretStr("123:test"))
    photo = {"image_url": "https://c.komissionki.ru/example/example_XL.webp", "file_id": "file"}
    for _ in range(2):
        response = await client.post("/media/cache", headers=headers(), json={"photos": [photo]})
        assert response.status_code == 200
    response = await client.post(
        "/media/cache",
        headers=headers(),
        json={
            "photos": [
                {**photo, "file_id": None},
            ]
        },
    )
    assert response.status_code == 200
    response = await client.post(
        "/media/cache",
        headers=headers(),
        json={
            "photos": [
                {**photo, "image_url": "https://evil.test/photo.jpg"},
            ]
        },
    )
    assert response.status_code == 422

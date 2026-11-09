from datetime import timedelta

import httpx
import pytest
from sqlalchemy import func, select

from app import service
from app.api import app
from app.config import settings
from app.control import alert, maintenance, scan_interval
from app.db import session_dependency, utcnow
from app.models import AdminAlert, Category, City, Notification, RuntimeControl, Source, User
from app.models import TestMode as DemoMode
from app.schemas import ListingPage, ProductData

pytestmark = pytest.mark.postgres


@pytest.fixture
async def api_client(session_factory, monkeypatch, tmp_path):
    def dependency():
        with session_factory() as db:
            yield db

    monkeypatch.setattr(settings(), "admin_telegram_ids", "123")
    monkeypatch.setattr(settings(), "backups_dir", str(tmp_path))
    monkeypatch.setattr(settings(), "host_disk_path", str(tmp_path))
    app.dependency_overrides[session_dependency] = dependency
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
        headers={"X-API-Key": settings().api_key.get_secret_value()},
    ) as client:
        yield client
    app.dependency_overrides.clear()


def setup(db):
    user = User(telegram_id=123, chat_id=123)
    other = User(telegram_id=456, chat_id=456)
    category = Category(name="Ноутбуки", path="/catalog/a/b/")
    db.add_all([user, other, category])
    db.flush()
    sub = service.add_category(db, user, category)
    db.add(City(name="Краснодар", hostname="xn--80aalwqglfe.komissionki.ru"))
    db.commit()
    return user, sub


def page(numbers, **kwargs):
    return ListingPage(
        products=[
            ProductData(
                external_id=str(n),
                title=f"MacBook {n}",
                price=100,
                url=f"https://komissionki.ru/catalog/a/b/p-{n}/",
                category_path="/catalog/a/b/",
                cities=["Краснодар"],
                condition="Хорошее",
                **kwargs,
            )
            for n in numbers
        ],
        total=len(numbers),
    )


async def test_status_explains_initialization_no_news_and_pause(api_client, db):
    user, sub = setup(db)
    result = (await api_client.get("/users/123/status")).json()
    assert result["subscriptions"][0]["reason"] == "initializing"
    service.apply_incremental_page(db, sub.source_id, page([1]), 300)
    db.commit()
    result = (await api_client.get("/users/123/status")).json()
    assert result["subscriptions"][0]["reason"] == "no_new_items"
    assert result["subscriptions"][0]["checked"] == 1
    user.active = False
    db.commit()
    result = (await api_client.get("/users/123/status")).json()
    assert result["subscriptions"][0]["reason"] == "paused"


async def test_filters_ownership_validation_and_live_matching(api_client, db):
    _, sub = setup(db)
    path = f"/users/123/subscriptions/category/{sub.id}/filters"
    response = await api_client.patch(
        path,
        json={
            "price_min": 50,
            "price_max": 200,
            "conditions": ["хорошее"],
            "cities": ["Краснодар"],
            "stop_words": ["запчасти"],
        },
    )
    assert response.status_code == 200
    assert (await api_client.patch(path.replace("/123/", "/456/"), json={})).status_code == 404
    assert (
        await api_client.patch(path, json={"price_min": 200, "price_max": 50})
    ).status_code == 422
    assert (await api_client.patch(path, json={"cities": ["Неизвестный город"]})).status_code == 422
    db.expire_all()
    service.apply_incremental_page(db, sub.source_id, page([1]), 300)
    db.commit()
    service.apply_incremental_page(db, sub.source_id, page([2, 1]), 300)
    db.commit()
    assert db.scalar(select(func.count()).select_from(Notification)) == 1
    assert (await api_client.get("/users/123/status")).json()["subscriptions"][0][
        "new_matching"
    ] == 1


async def test_queued_delivery_is_filtered_again_after_settings_change(api_client, db):
    _, sub = setup(db)
    service.apply_incremental_page(db, sub.source_id, page([1]), 300)
    db.commit()
    service.apply_incremental_page(db, sub.source_id, page([2, 1]), 300)
    db.commit()
    response = await api_client.patch(
        f"/users/123/subscriptions/category/{sub.id}/filters", json={"price_max": 50}
    )
    assert response.status_code == 200
    assert service.claim_notification(db) is None
    db.commit()
    assert db.scalar(select(Notification.status)) == "skipped"


async def test_admin_only_and_interval_auto_expiration(api_client, db):
    setup(db)
    assert (await api_client.get("/admin/456/status")).status_code == 403
    assert (
        await api_client.post("/admin/456/interval", json={"seconds": 300, "minutes": 15})
    ).status_code == 403
    response = await api_client.post("/admin/123/interval", json={"seconds": 300, "minutes": 15})
    assert response.status_code == 200
    db.expire_all()
    assert scan_interval(db) == 300
    control = db.get(RuntimeControl, 1)
    control.expires_at = utcnow() - timedelta(seconds=1)
    db.commit()
    maintenance(db)
    db.commit()
    assert scan_interval(db) == settings().scan_interval_seconds
    assert db.get(RuntimeControl, 1).interval_seconds is None


async def test_admin_test_run_claims_once_and_stops_automatically(api_client, db):
    user, _ = setup(db)
    assert (await api_client.post("/admin/456/test", json={"minutes": 15})).status_code == 403
    assert (await api_client.post("/admin/123/test", json={"minutes": 15})).status_code == 200
    assert (await api_client.post("/test-runs/claim")).status_code == 200
    assert (await api_client.post("/test-runs/claim")).status_code == 204
    db.expire_all()
    mode = db.get(DemoMode, user.id)
    mode.expires_at = utcnow() - timedelta(seconds=1)
    db.commit()
    assert (await api_client.post("/test-runs/claim")).status_code == 204
    maintenance(db)
    db.commit()
    assert db.get(DemoMode, user.id) is None


async def test_backup_request_has_admin_check_and_coalesces(api_client, db, tmp_path):
    setup(db)
    assert (await api_client.post("/admin/456/backup")).status_code == 403
    assert not (tmp_path / "request").exists()
    assert (await api_client.post("/admin/123/backup")).status_code == 200
    assert (await api_client.post("/admin/123/backup")).status_code == 200
    assert (tmp_path / "request").is_file()


async def test_alert_throttle_and_reactivation(api_client, db):
    setup(db)
    alert(db, "test", "warning", True)
    db.commit()
    assert len((await api_client.post("/admin-alerts/claim")).json()) == 1
    assert (await api_client.post("/admin-alerts/claim")).json() == []
    alert(db, "test", "resolved", False)
    db.commit()
    alert(db, "test", "again", True)
    db.commit()
    assert len((await api_client.post("/admin-alerts/claim")).json()) == 1
    assert db.get(AdminAlert, "test").active


async def test_site_error_is_visible_to_user(api_client, db):
    _, sub = setup(db)
    db.get(Source, sub.source_id).last_error = "Shop HTTP 429"
    db.commit()
    status = (await api_client.get("/users/123/status")).json()
    assert status["subscriptions"][0]["reason"] == "site_unavailable"
    db.get(Source, sub.source_id).last_error = "Hourly request budget"
    db.commit()
    status = (await api_client.get("/users/123/status")).json()
    assert status["subscriptions"][0]["reason"] == "rate_limited"

from datetime import timedelta

import pytest
from pydantic import SecretStr
from sqlalchemy import func, select

from app import service
from app.config import settings
from app.db import utcnow
from app.models import Category, Notification, PageCache, ParseJob, Source, TelegramPhoto, User
from app.parser import listing_url
from app.schemas import ListingPage, ProductData

pytestmark = pytest.mark.postgres
URL = "https://c.komissionki.ru/example/example_XL.webp"


def setup(db):
    user = User(telegram_id=123, chat_id=123)
    category = Category(name="Ноутбуки", path="/catalog/a/b/")
    db.add_all([user, category])
    db.flush()
    sub = service.add_category(db, user, category)
    db.commit()
    return user, sub


def cached(db, age=0):
    products = [
        ProductData(
            external_id=str(i),
            title=f"MacBook {i}",
            price=100,
            url=f"https://komissionki.ru/catalog/a/b/item-{i}/",
            category_path="/catalog/a/b/",
            images=[URL],
            published_at=utcnow() + timedelta(seconds=i),
        )
        for i in range(20)
    ]
    db.add(
        PageCache(
            url=listing_url("category", "/catalog/a/b/"),
            payload={
                "kind": "listing",
                **ListingPage(products=products, total=2000).model_dump(mode="json"),
            },
            fetched_at=utcnow() - timedelta(seconds=age),
        )
    )
    db.commit()


def test_fresh_snapshot_is_immediate_no_parse_jobs_or_baseline_changes(db):
    user, sub = setup(db)
    cached(db)
    result = service.preview_snapshot(db, user, 15)
    db.commit()
    assert result["has_snapshot"]
    assert len(result["products"]) == 15
    assert result["products"][0]["external_id"] == "19"
    assert result["refresh_queued"] == 0
    assert db.scalar(select(func.count()).select_from(ParseJob)) == 0
    assert db.scalar(select(func.count()).select_from(Notification)) == 0
    assert db.get(Source, sub.source_id).generation == 0


def test_stale_snapshot_is_returned_while_refresh_is_queued_once(db):
    user, _ = setup(db)
    cached(db, age=3600)
    one = service.preview_snapshot(db, user, 15)
    two = service.preview_snapshot(db, user, 15)
    db.commit()
    assert len(one["products"]) == len(two["products"]) == 15
    assert one["refresh_queued"] == 1
    assert db.scalar(select(func.count()).select_from(ParseJob)) == 1


def test_snapshot_attaches_only_current_bot_photo_references(db, monkeypatch):
    monkeypatch.setattr(settings(), "bot_token", SecretStr("123:test"))
    user, _ = setup(db)
    cached(db)
    db.add_all(
        [
            TelegramPhoto(bot_id=123, image_url=URL, file_id="correct-bot"),
            TelegramPhoto(bot_id=456, image_url=URL, file_id="wrong-bot"),
        ]
    )
    db.commit()
    result = service.preview_snapshot(db, user, 15)
    assert all(p["telegram_photo_cache"] == {URL: "correct-bot"} for p in result["products"])


def test_missing_snapshot_queues_refresh_without_claiming_it_exists(db):
    user, _ = setup(db)
    result = service.preview_snapshot(db, user, 15)
    db.commit()
    assert not result["has_snapshot"]
    assert result["products"] == []
    assert result["waiting"] == result["refresh_queued"] == 1

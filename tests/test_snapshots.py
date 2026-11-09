from datetime import timedelta
from unittest.mock import AsyncMock
from urllib.parse import parse_qs, urlsplit

import pytest
from sqlalchemy import func, select

from app import service, worker
from app.config import Settings
from app.db import utcnow
from app.models import Category, Notification, Product, Source, User
from app.schemas import ListingPage, ProductData

pytestmark = pytest.mark.postgres


def products(start, end):
    now = utcnow()
    return [
        ProductData(
            external_id=str(n),
            title=f"MacBook {n}",
            price=100,
            url=f"https://komissionki.ru/catalog/a/b/item-{n}/",
            category_path="/catalog/a/b/",
            published_at=now + timedelta(seconds=n),
        )
        for n in range(start, end)
    ]


def subscribe(db):
    user = User(telegram_id=123, chat_id=123)
    category = Category(name="Ноутбуки", path="/catalog/a/b/")
    db.add_all([user, category])
    db.flush()
    subscription = service.add_category(db, user, category)
    db.commit()
    return subscription.source_id


def test_initial_page_has_no_15_item_limit_and_does_not_notify(db):
    source_id = subscribe(db)
    first = ListingPage(products=products(1, 21), total=2600, next_cursor="ignored")
    service.apply_incremental_page(db, source_id, first, 1800)
    db.commit()
    assert db.scalar(select(func.count()).select_from(Product)) == 20
    assert set(db.scalars(select(Product.external_id))) == {str(n) for n in range(1, 21)}
    assert db.scalar(select(func.count()).select_from(Notification)) == 0
    source = db.get(Source, source_id)
    assert source.generation == 1
    assert source.cursor is None
    assert source.next_scan_at > utcnow() + timedelta(seconds=1790)


async def test_worker_fetches_one_first_page_and_resets_old_cursor(
    db, session_factory, monkeypatch
):
    source_id = subscribe(db)
    source = db.get(Source, source_id)
    source.cursor = "old-cursor"
    source.page_count = 100
    source.item_count = 2000
    source.page_fingerprints = ["stale"]
    db.commit()
    config = Settings()
    monkeypatch.setattr(worker, "settings", lambda: config)
    monkeypatch.setattr(worker, "sessions", lambda: session_factory)
    crawler = AsyncMock()
    crawler.fetch.return_value = ListingPage(
        products=products(1, 21),
        total=2600,
        next_cursor="next-page",
    ).model_dump(mode="json")
    await worker.scan_source(crawler, source_id)
    crawler.fetch.assert_awaited_once()
    assert "sa" not in parse_qs(urlsplit(crawler.fetch.call_args.args[0]).query)
    db.expire_all()
    source = db.get(Source, source_id)
    assert source.cursor is None
    assert source.generation == 1
    assert db.scalar(select(func.count()).select_from(Product)) == 20


def test_empty_snapshot_initializes_subscription(db):
    source_id = subscribe(db)
    service.apply_incremental_page(db, source_id, ListingPage(products=[], total=0), 1800)
    db.commit()
    assert db.get(Source, source_id).generation == 1
    assert db.scalar(select(func.count()).select_from(Notification)) == 0

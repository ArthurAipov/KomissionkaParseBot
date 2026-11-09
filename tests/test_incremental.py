from unittest.mock import AsyncMock

import pytest
from sqlalchemy import func, select

from app import service, worker
from app.config import settings
from app.models import Category, Notification, Source, User
from app.parser import ParseError
from app.schemas import ListingPage, ProductData

pytestmark = pytest.mark.postgres


def page(numbers, cursor=None, ordered=True):
    return ListingPage(
        products=[
            ProductData(
                external_id=str(n),
                title=f"MacBook {n}",
                price=100,
                url=f"https://komissionki.ru/catalog/a/b/item-{n}/",
                category_path="/catalog/a/b/",
            )
            for n in numbers
        ],
        total=1000,
        next_cursor=cursor,
        newest_first=ordered,
    )


def setup(db, kind="category"):
    user = User(telegram_id=123, chat_id=123)
    category = Category(name="Ноутбуки", path="/catalog/a/b/")
    db.add_all([user, category])
    db.flush()
    sub = (
        service.add_category(db, user, category)
        if kind == "category"
        else service.add_keyword(db, user, "macbook")
    )
    db.commit()
    service.apply_incremental_page(db, sub.source_id, page(range(100, 80, -1), "ignored"), 300)
    db.commit()
    return user, sub


def count(db):
    return db.scalar(select(func.count()).select_from(Notification))


def test_collects_45_new_items_across_pages_then_stops_on_known(db):
    _, sub = setup(db)
    assert not service.apply_incremental_page(
        db, sub.source_id, page(range(145, 125, -1), "A"), 300
    )
    db.commit()
    assert not service.apply_incremental_page(
        db, sub.source_id, page(range(125, 105, -1), "B"), 300
    )
    db.commit()
    assert service.apply_incremental_page(db, sub.source_id, page(range(105, 85, -1), "C"), 300)
    db.commit()
    source = db.get(Source, sub.source_id)
    assert count(db) == 45
    assert source.last_checked_count == 46
    assert source.last_new_count == 45
    assert source.last_stop_reason == "known_product"
    assert source.cursor is None


def test_known_in_other_category_is_not_a_boundary(db):
    user, sub = setup(db)
    other = service.add_keyword(db, user, "other")
    db.commit()
    service.apply_incremental_page(db, other.source_id, page([101]), 300)
    db.commit()
    assert not service.apply_incremental_page(db, sub.source_id, page([102, 101], "A"), 300)
    db.commit()
    assert count(db) == 2


def test_partial_pass_restart_does_not_stop_on_items_from_same_pass(db):
    _, sub = setup(db)
    service.apply_incremental_page(db, sub.source_id, page([104, 103], "A"), 300)
    db.commit()
    db.expire_all()
    assert service.apply_incremental_page(db, sub.source_id, page([103, 102, 101, 100], "B"), 300)
    db.commit()
    assert count(db) == 4


def test_relevance_ranked_search_does_not_stop_before_newer_unknown_items(db):
    _, sub = setup(db, "search")
    assert not service.apply_incremental_page(db, sub.source_id, page([100, 101], "A", False), 300)
    db.commit()
    assert service.apply_incremental_page(db, sub.source_id, page([102]), 300)
    db.commit()
    assert count(db) == 2
    assert db.get(Source, sub.source_id).last_stop_reason == "end"


def test_unsafe_category_page_also_disables_early_stop(db):
    _, sub = setup(db)
    service.apply_incremental_page(db, sub.source_id, page([100, 101], ordered=False), 300)
    db.commit()
    assert count(db) == 1


def test_repeated_page_is_error_not_success(db):
    _, sub = setup(db)
    service.apply_incremental_page(db, sub.source_id, page([102, 101], "A"), 300)
    db.commit()
    with pytest.raises(ParseError):
        service.apply_incremental_page(db, sub.source_id, page([102, 101], "B"), 300)
    db.rollback()
    assert db.get(Source, sub.source_id).generation == 1


async def test_worker_does_not_request_another_page_after_boundary(
    db, session_factory, monkeypatch
):
    _, sub = setup(db)
    monkeypatch.setattr(worker, "sessions", lambda: session_factory)
    crawler = AsyncMock()
    crawler.fetch.return_value = page([103, 102, 101, 100], "A").model_dump(mode="json")
    await worker.scan_source(crawler, sub.source_id)
    crawler.fetch.assert_awaited_once()
    db.expire_all()
    assert count(db) == 3


async def test_worker_continues_three_pages_then_yields_with_cursor(
    db, session_factory, monkeypatch
):
    _, sub = setup(db)
    monkeypatch.setattr(worker, "sessions", lambda: session_factory)
    monkeypatch.setattr(settings(), "crawl_pages_per_turn", 3)
    crawler = AsyncMock()
    crawler.fetch.side_effect = [
        page([106, 105], "A"),
        page([104, 103], "B"),
        page([102, 101], "C"),
    ]
    crawler.fetch.side_effect = [p.model_dump(mode="json") for p in crawler.fetch.side_effect]
    await worker.scan_source(crawler, sub.source_id)
    db.expire_all()
    assert crawler.fetch.await_count == 3
    assert db.get(Source, sub.source_id).cursor == "C"
    assert db.get(Source, sub.source_id).generation == 1

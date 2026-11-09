from datetime import timedelta
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from app import service
from app.db import utcnow
from app.models import Category, Notification, Product, Source, User
from app.parser import ParseError
from app.schemas import ListingPage, ProductData

pytestmark = pytest.mark.postgres


def product(number: int, published_at=None):
    return ProductData(
        external_id=str(number),
        url=f"https://komissionki.ru/catalog/a/b/test-{number}/",
        title=f"MacBook {number}",
        price=54900,
        category_path="/catalog/a/b/",
        published_at=published_at,
    )


def page(*numbers, cursor=None, total=None):
    return ListingPage(
        products=[product(n) for n in numbers],
        total=total if total is not None else len(numbers),
        next_cursor=cursor,
    )


def setup(db):
    user = User(telegram_id=123, chat_id=123)
    category = Category(name="Ноутбуки", path="/catalog/a/b/")
    db.add_all([user, category])
    db.flush()
    subscription = service.add_category(db, user, category)
    db.commit()
    return user, category, subscription


def notifications(db):
    return db.scalar(select(func.count()).select_from(Notification))


def test_baseline_new_items_and_restart_do_not_duplicate(db):
    _, _, sub = setup(db)
    service.apply_page(db, sub.source_id, page(1, 2), 1800)
    db.commit()
    assert notifications(db) == 0
    service.apply_page(db, sub.source_id, page(3, 1, 2), 1800)
    db.commit()
    assert notifications(db) == 1
    db.expire_all()  # Re-read state as a restarted worker would.
    service.apply_page(db, sub.source_id, page(3, 1, 2), 1800)
    db.commit()
    assert notifications(db) == 1


def test_overlapping_search_and_category_send_once(db):
    user, _, cat_sub = setup(db)
    keyword = service.add_keyword(db, user, " MACBOOK ")
    db.commit()
    for source_id in (cat_sub.source_id, keyword.source_id):
        service.apply_page(db, source_id, page(1), 1800)
        db.commit()
        service.apply_page(db, source_id, page(2, 1), 1800)
        db.commit()
    assert notifications(db) == 1


def test_second_user_gets_own_baseline(db):
    _, category, sub = setup(db)
    service.apply_page(db, sub.source_id, page(1), 1800)
    db.commit()
    second_user = User(telegram_id=456, chat_id=456)
    db.add(second_user)
    db.flush()
    second_sub = service.add_category(db, second_user, category)
    db.commit()
    assert second_sub.source_id == sub.source_id
    service.apply_page(db, sub.source_id, page(2, 1), 1800)
    db.commit()
    assert notifications(db) == 1
    service.apply_page(db, sub.source_id, page(3, 2, 1), 1800)
    db.commit()
    assert notifications(db) == 3


def test_baseline_must_cover_all_pages(db):
    _, _, sub = setup(db)
    service.apply_page(db, sub.source_id, page(1, cursor="next", total=2), 1800)
    db.commit()
    assert db.get(Source, sub.source_id).generation == 0
    service.apply_page(db, sub.source_id, page(2, total=2), 1800)
    db.commit()
    assert db.get(Source, sub.source_id).generation == 1
    assert notifications(db) == 0


def test_subscribing_mid_scan_skips_remaining_baseline(db):
    _, category, sub = setup(db)
    service.apply_page(db, sub.source_id, page(1, cursor="next", total=2), 1800)
    db.commit()
    user = User(telegram_id=456, chat_id=456)
    db.add(user)
    db.flush()
    service.add_category(db, user, category)
    db.commit()
    service.apply_page(db, sub.source_id, page(2, total=2), 1800)
    db.commit()
    assert notifications(db) == 0


def test_repeated_page_does_not_complete_baseline(db):
    _, _, sub = setup(db)
    service.apply_page(db, sub.source_id, page(1, cursor="next", total=2), 1800)
    db.commit()
    with pytest.raises(ParseError):
        service.apply_page(db, sub.source_id, page(1, total=2), 1800)
    db.rollback()
    assert db.get(Source, sub.source_id).generation == 0


def test_old_product_reentering_results_is_not_new(db):
    _, _, sub = setup(db)
    service.apply_page(db, sub.source_id, page(1), 1800)
    db.commit()
    old = product(2, utcnow() - timedelta(days=5))
    service.apply_page(db, sub.source_id, ListingPage(products=[old], total=1), 1800)
    db.commit()
    assert notifications(db) == 0


def test_paused_users_do_not_get_backlog(db):
    user, _, sub = setup(db)
    service.apply_page(db, sub.source_id, page(1), 1800)
    user.active = False
    db.commit()
    service.apply_page(db, sub.source_id, page(2, 1), 1800)
    db.commit()
    assert notifications(db) == 0
    user.active = True
    db.commit()
    service.apply_page(db, sub.source_id, page(3, 2, 1), 1800)
    db.commit()
    assert notifications(db) == 1


def test_claim_once_and_ambiguous_expired_send_is_not_retried(db):
    _, _, sub = setup(db)
    service.apply_page(db, sub.source_id, page(1), 1800)
    db.commit()
    service.apply_page(db, sub.source_id, page(2, 1), 1800)
    db.commit()
    claim = service.claim_notification(db)
    db.commit()
    assert claim["product"]["external_id"] == "2"
    assert service.claim_notification(db) is None
    db.get(Notification, claim["id"]).lease_until = utcnow() - timedelta(seconds=1)
    db.commit()
    assert service.claim_notification(db) is None
    db.commit()
    db.expire_all()
    assert db.get(Notification, claim["id"]).status == "uncertain"


def test_removing_subscription_skips_pending_delivery(db):
    _, _, sub = setup(db)
    service.apply_page(db, sub.source_id, page(1), 1800)
    db.commit()
    service.apply_page(db, sub.source_id, page(2, 1), 1800)
    db.commit()
    db.delete(sub)
    db.commit()
    assert service.claim_notification(db) is None
    db.commit()
    assert db.scalar(select(Notification.status)) == "skipped"


def test_same_keyword_is_one_source_for_multiple_users(db):
    user, _, _ = setup(db)
    other = User(telegram_id=456, chat_id=456)
    db.add(other)
    db.flush()
    one = service.add_keyword(db, user, "MacBook   Pro")
    two = service.add_keyword(db, other, " macbook pro ")
    db.commit()
    assert one.source_id == two.source_id


def test_duplicate_parse_requests_share_one_job(db):
    url = "https://komissionki.ru/catalog/"
    one = service.enqueue_parse(db, url, 300)
    two = service.enqueue_parse(db, url, 300)
    assert one.id == two.id


def test_price_update_does_not_enqueue_new_notification(db):
    _, _, sub = setup(db)
    service.apply_page(db, sub.source_id, page(1), 1800)
    db.commit()
    changed = product(1).model_copy(update={"price": Decimal("100")})
    service.apply_page(db, sub.source_id, ListingPage(products=[changed], total=1), 1800)
    db.commit()
    assert notifications(db) == 0
    assert db.scalar(select(Product.price)) == 100


def test_peek_does_not_take_a_delivery_lease(db):
    _, _, sub = setup(db)
    service.apply_page(db, sub.source_id, page(1), 1800)
    db.commit()
    service.apply_page(db, sub.source_id, page(2, 1), 1800)
    db.commit()
    peek = service.peek_notification(db)
    db.commit()
    notification = db.get(Notification, peek["id"])
    assert notification.status == "pending"
    assert notification.attempts == 0
    assert notification.lease_token is None
    claimed = service.claim_notification(db)
    assert claimed["id"] == peek["id"]

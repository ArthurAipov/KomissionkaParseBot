import hashlib
import secrets
from datetime import timedelta

from sqlalchemy import exists, func, or_, select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.config import settings
from app.control import scan_interval
from app.db import utcnow
from app.filters import matches
from app.models import (
    Category,
    CustomCategory,
    Notification,
    PageCache,
    ParseJob,
    Product,
    Source,
    SourceProduct,
    TelegramPhoto,
    User,
    UserCategory,
)
from app.parser import ParseError, listing_url
from app.schemas import CategoryData, ListingPage, normalize_query


def user_by_telegram(db: Session, telegram_id: int) -> User | None:
    return db.scalar(select(User).where(User.telegram_id == telegram_id))


def sync_categories(db: Session, entries: list[CategoryData]) -> None:
    for entry in sorted(entries, key=lambda c: c.path.count("/")):
        parent = db.scalar(select(Category.id).where(Category.path == entry.parent_path))
        db.execute(
            insert(Category)
            .values(name=entry.name, path=entry.path, parent_id=parent)
            .on_conflict_do_update(
                index_elements=[Category.path],
                set_={
                    "name": entry.name,
                    "parent_id": parent,
                },
            )
        )


def get_source(db: Session, kind: str, value: str) -> Source:
    if kind == "search":
        value = normalize_query(value)
    key = f"{kind}:{value}"
    db.execute(
        insert(Source)
        .values(key=key, kind=kind, value=value)
        .on_conflict_do_nothing(index_elements=[Source.key])
    )
    # Serializes subscription creation with the page commit. A new subscriber gets a baseline
    # from a complete scan, even if they subscribe halfway through the current scan.
    return db.scalar(select(Source).where(Source.key == key).with_for_update())


def add_category(db: Session, user: User, category: Category) -> UserCategory:
    source = get_source(db, "category", category.path)
    db.execute(
        insert(UserCategory)
        .values(
            user_id=user.id,
            category_id=category.id,
            source_id=source.id,
            baseline_generation=source.generation + 1,
        )
        .on_conflict_do_nothing(index_elements=[UserCategory.user_id, UserCategory.category_id])
    )
    return db.scalar(
        select(UserCategory).where(
            UserCategory.user_id == user.id, UserCategory.category_id == category.id
        )
    )


def add_keyword(db: Session, user: User, name: str) -> CustomCategory:
    name = normalize_query(name)
    source = get_source(db, "search", name)
    db.execute(
        insert(CustomCategory)
        .values(
            user_id=user.id,
            name=name,
            source_id=source.id,
            baseline_generation=source.generation + 1,
        )
        .on_conflict_do_nothing(index_elements=[CustomCategory.user_id, CustomCategory.name])
    )
    return db.scalar(
        select(CustomCategory).where(CustomCategory.user_id == user.id, CustomCategory.name == name)
    )


def source_has_subscribers():
    return or_(
        exists(select(UserCategory.id).where(UserCategory.source_id == Source.id)),
        exists(select(CustomCategory.id).where(CustomCategory.source_id == Source.id)),
    )


def source_subscriptions(db: Session, source_id: int):
    return [
        *db.scalars(select(UserCategory).where(UserCategory.source_id == source_id)),
        *db.scalars(select(CustomCategory).where(CustomCategory.source_id == source_id)),
    ]


def apply_page(
    db: Session, source_id: int, page: ListingPage, interval: int, incremental: bool = False
) -> bool:
    source = db.get(Source, source_id, with_for_update=True)
    generation = source.generation + 1
    fingerprint = hashlib.sha256(
        ",".join(sorted(p.external_id for p in page.products)).encode()
    ).hexdigest()
    if page.products and fingerprint in source.page_fingerprints:
        raise ParseError("Shop repeated a page; scan has not been marked complete")
    if page.next_cursor and page.next_cursor == source.cursor:
        raise ParseError("Shop repeated its cursor; scan has not been marked complete")
    if not page.products and page.total > 0 and source.page_count == 0:
        raise ParseError("Empty first page with a nonzero total")
    if not page.products and page.total > source.item_count:
        raise ParseError("Empty page before the expected end of the listing")
    now = utcnow()
    subscriptions = source_subscriptions(db, source.id)
    if source.scan_started_at is None:
        source.scan_started_at = now
        source.scan_ordered = source.kind != "search"
        source.scan_new_count = 0
        for subscription in subscriptions:
            subscription.last_matched_count = 0
    source.scan_ordered = source.scan_ordered and page.newest_first
    known = (
        set(
            db.scalars(
                select(Product.external_id)
                .join(SourceProduct, SourceProduct.product_id == Product.id)
                .where(
                    SourceProduct.source_id == source.id,
                    SourceProduct.first_generation < generation,
                    Product.external_id.in_([p.external_id for p in page.products]),
                )
            )
        )
        if incremental
        else set()
    )
    boundary = False
    checked = 0
    active_users = set(db.scalars(select(User.id).where(User.active.is_(True))))
    for data in page.products:
        checked += 1
        if (
            incremental
            and source.generation > 0
            and source.scan_ordered
            and data.external_id in known
        ):
            boundary = True
            break
        values = data.model_dump()
        statement = insert(Product).values(**values)
        product_id = db.scalar(
            statement.on_conflict_do_update(
                index_elements=[Product.external_id],
                set_={k: v for k, v in values.items() if k != "external_id"},
            ).returning(Product.id)
        )
        first = db.scalar(
            insert(SourceProduct)
            .values(
                source_id=source.id,
                product_id=product_id,
                first_generation=generation,
            )
            .on_conflict_do_nothing()
            .returning(SourceProduct.product_id)
        )
        if first is None:
            continue
        if source.generation > 0:
            source.scan_new_count += 1
        for subscription in subscriptions:
            if subscription.baseline_generation >= generation:
                continue
            # A years-old listing entering a search because its description/price changed
            # must not become a "new product" notification.
            if data.published_at and data.published_at <= subscription.created_at:
                continue
            if not matches(data, subscription.filters):
                continue
            subscription.last_matched_count += 1
            if subscription.user_id not in active_users:
                continue
            db.execute(
                insert(Notification)
                .values(
                    user_id=subscription.user_id,
                    product_id=product_id,
                )
                .on_conflict_do_nothing(
                    index_elements=[Notification.user_id, Notification.product_id]
                )
            )
    source.page_count += 1
    source.item_count += checked
    bootstrap = incremental and source.generation == 0
    cursor = None if bootstrap or boundary else page.next_cursor
    source.total, source.cursor = page.total, cursor
    source.failures, source.last_error = 0, None
    source.page_fingerprints = [*source.page_fingerprints, fingerprint]
    if cursor:
        source.next_scan_at = now
        return False
    source.generation = generation
    source.last_checked_count = source.item_count
    source.last_new_count = source.scan_new_count
    source.last_stop_reason = "initial" if bootstrap else "known_product" if boundary else "end"
    source.last_success_at = now
    source.next_scan_at = max(now, source.scan_started_at + timedelta(seconds=interval))
    source.scan_started_at = None
    source.cursor = None
    source.page_count, source.item_count = 0, 0
    source.page_fingerprints = []
    return True


def apply_incremental_page(db: Session, source_id: int, page: ListingPage, interval: int) -> bool:
    source = db.get(Source, source_id, with_for_update=True)
    if source.generation == 0:
        source.cursor = source.scan_started_at = None
        source.page_count = source.item_count = 0
        source.page_fingerprints = []
    return apply_page(db, source_id, page, interval, incremental=True)


def enqueue_parse(db: Session, url: str, cache_seconds: int) -> ParseJob:
    # Prevent duplicate API requests from creating a burst of identical crawler jobs.
    lock_id = int.from_bytes(hashlib.sha256(url.encode()).digest()[:8], "big", signed=True)
    db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": lock_id})
    job = db.scalar(
        select(ParseJob)
        .where(
            ParseJob.url == url,
            or_(
                ParseJob.status.in_(["pending", "running"]),
                (ParseJob.status == "done")
                & (ParseJob.finished_at > utcnow() - timedelta(seconds=cache_seconds)),
            ),
        )
        .order_by(ParseJob.id.desc())
        .limit(1)
    )
    if job is None:
        pending = db.scalar(
            select(func.count())
            .select_from(ParseJob)
            .where(ParseJob.status.in_(["pending", "running"]))
        )
        if pending >= 100:
            raise ValueError("Parse queue is full")
        job = ParseJob(url=url)
        db.add(job)
        db.flush()
    return job


def product_payload(product: Product) -> dict:
    # Keep SQLAlchemy internals and timestamps that are irrelevant to Telegram out of the API.
    return {
        "external_id": product.external_id,
        "url": product.url,
        "title": product.title,
        "price": str(product.price),
        "condition": product.condition,
        "description": product.description,
        "images": product.images,
        "cities": product.cities,
        "category_path": product.category_path,
        "telegram_file_ids": product.telegram_file_ids,
        "published_at": product.published_at.isoformat() if product.published_at else None,
    }


def attach_photo_references(db: Session, products: list[dict]) -> list[dict]:
    urls = {url for p in products for url in p.get("images", [])[:10]}
    references = (
        dict(
            db.execute(
                select(TelegramPhoto.image_url, TelegramPhoto.file_id).where(
                    TelegramPhoto.bot_id == settings().telegram_bot_id,
                    TelegramPhoto.image_url.in_(urls),
                )
            ).all()
        )
        if urls
        else {}
    )
    return [
        {
            **p,
            "telegram_photo_cache": {
                url: references[url] for url in p.get("images", [])[:10] if url in references
            },
            # The URL-based mapping supersedes legacy unscoped product-level file IDs.
            "telegram_file_ids": [],
        }
        for p in products
    ]


def preview_snapshot(db: Session, user: User, limit: int) -> dict:
    subscriptions = [
        *db.scalars(select(UserCategory).where(UserCategory.user_id == user.id)),
        *db.scalars(select(CustomCategory).where(CustomCategory.user_id == user.id)),
    ]
    source_ids = set(
        db.scalars(select(UserCategory.source_id).where(UserCategory.user_id == user.id))
    )
    source_ids.update(
        db.scalars(select(CustomCategory.source_id).where(CustomCategory.user_id == user.id))
    )
    products: dict[str, dict] = {}
    timestamps = []
    waiting = 0
    refresh_queued = 0
    for source in db.scalars(select(Source).where(Source.id.in_(source_ids)).order_by(Source.id)):
        url = listing_url(source.kind, source.value)
        cache = db.get(PageCache, url)
        if cache and cache.payload.get("kind") == "listing":
            timestamps.append(cache.fetched_at)
            for product in cache.payload.get("products", []):
                if any(
                    s.source_id == source.id and matches(product, s.filters) for s in subscriptions
                ):
                    products[product["external_id"]] = product
        else:
            waiting += 1
        if (
            cache is None
            or cache.payload.get("kind") != "listing"
            or cache.fetched_at + timedelta(seconds=scan_interval(db)) <= utcnow()
        ):
            try:
                enqueue_parse(db, url, settings().page_cache_seconds)
                refresh_queued += 1
            except ValueError:
                # A full manual queue must not prevent reading an already saved snapshot.
                pass
    ordered = sorted(products.values(), key=lambda p: p.get("published_at") or "", reverse=True)
    return {
        "products": attach_photo_references(db, ordered[:limit]),
        "has_snapshot": bool(timestamps),
        "snapshot_at": min(timestamps).isoformat() if timestamps else None,
        "waiting": waiting,
        "refresh_queued": refresh_queued,
    }


def notification_relevant(db: Session, notification: Notification) -> bool:
    product = db.get(Product, notification.product_id, populate_existing=True)
    for table in (UserCategory, CustomCategory):
        candidates = db.scalars(
            select(table)
            .execution_options(populate_existing=True)
            .join(SourceProduct, SourceProduct.source_id == table.source_id)
            .where(
                table.user_id == notification.user_id,
                SourceProduct.product_id == notification.product_id,
                SourceProduct.first_generation > table.baseline_generation,
            )
        )
        for subscription in candidates:
            if matches(product, subscription.filters):
                return True
    return False


def peek_notification(db: Session) -> dict | None:
    """Read one upcoming product for preparation without claiming or sending it."""
    candidates = db.scalars(
        select(Notification)
        .join(User)
        .where(
            Notification.status == "pending",
            Notification.next_attempt_at <= utcnow(),
            User.active.is_(True),
        )
        .order_by(Notification.id)
        .limit(50)
    )
    for notification in candidates:
        if notification_relevant(db, notification):
            product = db.get(Product, notification.product_id)
            return {
                "id": notification.id,
                "product": attach_photo_references(db, [product_payload(product)])[0],
            }
    return None


def claim_notification(db: Session) -> dict | None:
    now = utcnow()
    # Telegram offers no idempotency key. Expired sends may already have reached Telegram;
    # do not retry them automatically and risk sending a duplicate album.
    db.execute(
        update(Notification)
        .where(
            Notification.status == "sending",
            Notification.lease_until < now,
        )
        .values(status="uncertain", last_error="Send lease expired; delivery outcome unknown")
    )
    for _ in range(50):
        notification = db.scalar(
            select(Notification)
            .join(User)
            .where(
                Notification.status == "pending",
                Notification.next_attempt_at <= now,
                User.active.is_(True),
            )
            .order_by(Notification.id)
            .with_for_update(skip_locked=True, of=Notification)
            .limit(1)
        )
        if notification is None:
            return None
        if not notification_relevant(db, notification):
            notification.status = "skipped"
            db.flush()
            continue
        user = db.get(User, notification.user_id)
        product = db.get(Product, notification.product_id)
        notification.status = "sending"
        notification.attempts += 1
        notification.lease_token = secrets.token_hex(16)
        notification.lease_until = now + timedelta(minutes=5)
        return {
            "id": notification.id,
            "chat_id": user.chat_id,
            "lease_token": notification.lease_token,
            "product": attach_photo_references(db, [product_payload(product)])[0],
        }
    return None

from contextlib import asynccontextmanager
from datetime import timedelta

from fastapi import FastAPI, HTTPException, Query, Response
from sqlalchemy import delete, func, select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app import service
from app.auth import Auth, Db, require_user
from app.config import configure_logging, settings
from app.db import utcnow
from app.models import (
    Category,
    CrawlerState,
    CustomCategory,
    Notification,
    ParseJob,
    Source,
    TelegramPhoto,
    User,
    UserCategory,
)
from app.parser import BASE_URL, image_url, listing_url, safe_shop_url
from app.schemas import (
    CategoryInput,
    DeliveryUpdate,
    KeywordInput,
    PhotoReferences,
    RegisterUser,
    UserUpdate,
    UserView,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging()
    yield


app = FastAPI(title="Komissionki Watcher API", version="0.1.0", lifespan=lifespan)


@app.get("/health")
def health(db: Db):
    try:
        db.execute(text("SELECT 1 FROM crawler_state WHERE id = 1"))
    except Exception as exc:
        raise HTTPException(503, "Database is not ready") from exc
    return {"status": "ok"}


@app.get("/status", dependencies=[Auth])
def status(db: Db):
    state = db.get(CrawlerState, 1)
    return {
        "worker_heartbeat_at": state.heartbeat_at,
        "catalog_synced_at": state.catalog_synced_at,
        "blocked_until": state.blocked_until,
        "block_reason": state.block_reason,
        "hour_requests": state.window_requests,
        "robots_mode": "strict" if settings().respect_robots else "cautious_public_html",
        "snapshot_limit": settings().snapshot_limit,
        "uncertain_notifications": db.scalar(
            select(func.count()).select_from(Notification).where(Notification.status == "uncertain")
        ),
    }


@app.post("/users", response_model=UserView, dependencies=[Auth])
def register(body: RegisterUser, db: Db):
    if body.chat_id != body.telegram_id:
        raise HTTPException(422, "Only private Telegram chats are supported")
    user = db.scalar(
        insert(User)
        .values(**body.model_dump())
        .on_conflict_do_update(
            index_elements=[User.telegram_id],
            set_={"chat_id": body.chat_id, "username": body.username},
        )
        .returning(User)
    )
    db.commit()
    return user


@app.patch("/users/{telegram_id}", response_model=UserView, dependencies=[Auth])
def update_user(telegram_id: int, body: UserUpdate, db: Db):
    user = require_user(db, telegram_id)
    user.active = body.active
    if not body.active:
        db.execute(
            update(Notification)
            .where(Notification.user_id == user.id, Notification.status == "pending")
            .values(status="skipped")
        )
    db.commit()
    return user


@app.get("/categories", dependencies=[Auth])
def categories(db: Db):
    return [
        {"id": c.id, "name": c.name, "path": c.path, "parent_id": c.parent_id}
        for c in db.scalars(select(Category).order_by(Category.name))
    ]


def subscription_view(subscription, source: Source, name: str, kind: str):
    return {
        "id": subscription.id,
        "name": name,
        "kind": kind,
        "source_id": source.id,
        "category_id": subscription.category_id if kind == "category" else None,
        "ready": source.generation >= subscription.baseline_generation,
        "last_success_at": source.last_success_at,
        "last_error": source.last_error,
        "pages_in_current_scan": source.page_count,
        "items_in_current_scan": source.item_count,
        "total": source.total,
        "next_scan_at": source.next_scan_at,
        "filters": subscription.filters,
        "last_matched_count": subscription.last_matched_count,
    }


@app.get("/users/{telegram_id}/subscriptions", dependencies=[Auth])
def subscriptions(telegram_id: int, db: Db):
    user = require_user(db, telegram_id)
    result = []
    for subscription, source, category in db.execute(
        select(UserCategory, Source, Category)
        .join(Source, Source.id == UserCategory.source_id)
        .join(Category, Category.id == UserCategory.category_id)
        .where(UserCategory.user_id == user.id)
    ):
        result.append(subscription_view(subscription, source, category.name, "category"))
    for subscription, source in db.execute(
        select(CustomCategory, Source)
        .join(Source, Source.id == CustomCategory.source_id)
        .where(CustomCategory.user_id == user.id)
    ):
        result.append(subscription_view(subscription, source, subscription.name, "search"))
    return {"active": user.active, "subscriptions": result}


@app.post("/users/{telegram_id}/preview", dependencies=[Auth])
def saved_preview(telegram_id: int, db: Db):
    user = require_user(db, telegram_id)
    result = service.preview_snapshot(db, user, settings().snapshot_limit)
    db.commit()
    return result


@app.post("/media/cache", dependencies=[Auth])
def save_photo_references(body: PhotoReferences, db: Db):
    bot_id = settings().telegram_bot_id
    if not bot_id:
        raise HTTPException(503, "Telegram bot is not configured")
    if any(not image_url(photo.image_url) for photo in body.photos):
        raise HTTPException(422, "Only shop CDN image URLs are accepted")
    for photo in body.photos:
        if photo.file_id is None:
            db.execute(
                delete(TelegramPhoto).where(
                    TelegramPhoto.bot_id == bot_id,
                    TelegramPhoto.image_url == photo.image_url,
                )
            )
        else:
            db.execute(
                insert(TelegramPhoto)
                .values(
                    bot_id=bot_id,
                    image_url=photo.image_url,
                    file_id=photo.file_id,
                )
                .on_conflict_do_update(
                    index_elements=[TelegramPhoto.bot_id, TelegramPhoto.image_url],
                    set_={"file_id": photo.file_id, "updated_at": utcnow()},
                )
            )
    db.commit()
    return {"saved": len(body.photos)}


def check_subscription_limit(db: Session, user: User) -> None:
    # Per-user row lock makes simultaneous additions obey the same limit.
    db.get(User, user.id, with_for_update=True)
    count = sum(
        db.scalar(select(func.count()).select_from(table).where(table.user_id == user.id))
        for table in (UserCategory, CustomCategory)
    )
    if count >= 20:
        raise HTTPException(409, "Maximum 20 subscriptions per user")


@app.post("/users/{telegram_id}/categories", dependencies=[Auth])
def subscribe_category(telegram_id: int, body: CategoryInput, db: Db):
    user = require_user(db, telegram_id)
    category = db.get(Category, body.category_id)
    if category is None:
        raise HTTPException(404, "Unknown category")
    existing = db.scalar(
        select(UserCategory).where(
            UserCategory.user_id == user.id, UserCategory.category_id == category.id
        )
    )
    if existing is None:
        check_subscription_limit(db, user)
    subscription = service.add_category(db, user, category)
    source = db.get(Source, subscription.source_id)
    db.commit()
    return subscription_view(subscription, source, category.name, "category")


@app.post("/users/{telegram_id}/keywords", dependencies=[Auth])
def subscribe_keyword(telegram_id: int, body: KeywordInput, db: Db):
    user = require_user(db, telegram_id)
    existing = db.scalar(
        select(CustomCategory).where(
            CustomCategory.user_id == user.id, CustomCategory.name == body.name
        )
    )
    if existing is None:
        check_subscription_limit(db, user)
    subscription = service.add_keyword(db, user, body.name)
    source = db.get(Source, subscription.source_id)
    db.commit()
    return subscription_view(subscription, source, subscription.name, "search")


@app.delete("/users/{telegram_id}/subscriptions/{kind}/{subscription_id}", dependencies=[Auth])
def unsubscribe(telegram_id: int, kind: str, subscription_id: int, db: Db):
    user = require_user(db, telegram_id)
    table = {"category": UserCategory, "search": CustomCategory}.get(kind)
    if table is None:
        raise HTTPException(422, "Invalid subscription kind")
    db.execute(delete(table).where(table.id == subscription_id, table.user_id == user.id))
    db.commit()
    return {"deleted": True}


def job_response(job: ParseJob, response: Response):
    response.status_code = (
        200 if job.status == "done" else 202 if job.status in ("pending", "running") else 502
    )
    response.headers["Location"] = f"/parse/jobs/{job.id}"
    if response.status_code == 202:
        response.headers["Retry-After"] = "5"
    return {"id": job.id, "status": job.status, "result": job.result, "error": job.error}


def create_job(db: Session, url: str, response: Response):
    try:
        job = service.enqueue_parse(db, url, settings().page_cache_seconds)
    except ValueError as exc:
        raise HTTPException(429, str(exc)) from exc
    db.commit()
    return job_response(job, response)


@app.get("/parse/catalog", dependencies=[Auth])
def parse_catalog(db: Db, response: Response):
    return create_job(db, BASE_URL + "/catalog/", response)


@app.get("/parse/search", dependencies=[Auth])
def parse_search(
    db: Db,
    response: Response,
    query: str = Query(min_length=1, max_length=120),
    cursor: str | None = Query(None, max_length=2048),
):
    try:
        url = listing_url("search", query, cursor)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    return create_job(db, url, response)


@app.get("/parse/category", dependencies=[Auth])
def parse_category(
    db: Db,
    response: Response,
    category_id: int = Query(gt=0),
    cursor: str | None = Query(None, max_length=2048),
):
    category = db.get(Category, category_id)
    if category is None:
        raise HTTPException(404, "Unknown category")
    try:
        url = listing_url("category", category.path, cursor)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    return create_job(db, url, response)


@app.get("/parse/product", dependencies=[Auth])
def parse_product(db: Db, response: Response, url: str = Query(max_length=1500)):
    try:
        url = safe_shop_url(url)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    return create_job(db, url, response)


@app.get("/parse/jobs/{job_id}", dependencies=[Auth])
def get_job(job_id: int, db: Db, response: Response):
    job = db.get(ParseJob, job_id)
    if job is None:
        raise HTTPException(404, "Unknown parse job")
    return job_response(job, response)


@app.get("/notifications/peek", dependencies=[Auth])
def peek(db: Db):
    result = service.peek_notification(db)
    return result if result else Response(status_code=204)


@app.post("/notifications/claim", dependencies=[Auth])
def claim(db: Db, response: Response):
    notification = service.claim_notification(db)
    db.commit()
    if notification is None:
        return Response(status_code=204)
    return notification


@app.post("/notifications/{notification_id}/result", dependencies=[Auth])
def delivery_result(notification_id: int, body: DeliveryUpdate, db: Db):
    notification = db.get(Notification, notification_id, with_for_update=True)
    if notification is None:
        raise HTTPException(404, "Unknown notification")
    if notification.lease_token != body.lease_token:
        raise HTTPException(409, "Lease token mismatch")
    if notification.status != "sending":
        return {"status": notification.status}
    notification.last_error = body.error
    notification.lease_until = None
    if body.status == "sent":
        notification.status, notification.sent_at = "sent", utcnow()
        notification.telegram_message_ids = body.message_ids
        if "photo_file_ids" in body.model_fields_set:
            from app.models import Product

            db.get(Product, notification.product_id).telegram_file_ids = body.photo_file_ids
    elif body.status == "retry":
        notification.status = "pending" if notification.attempts < 8 else "failed"
        notification.next_attempt_at = utcnow() + timedelta(seconds=body.retry_after)
    elif body.status == "blocked":
        notification.status = "failed"
        db.get(User, notification.user_id).active = False
        db.execute(
            update(Notification)
            .where(Notification.user_id == notification.user_id, Notification.status == "pending")
            .values(status="skipped")
        )
    else:
        notification.status = body.status
    db.commit()
    return {"status": notification.status}


from app.routes import router as management_router  # noqa: E402

app.include_router(management_router)

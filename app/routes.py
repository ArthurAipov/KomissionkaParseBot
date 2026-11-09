from datetime import timedelta
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query, Response
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert

from app import service
from app.auth import Auth, Db, require_admin, require_user
from app.config import settings
from app.control import backup_status, reschedule, scan_interval, set_interval
from app.db import utcnow
from app.models import (
    AdminAlert,
    Category,
    City,
    CrawlerState,
    CustomCategory,
    Notification,
    RuntimeControl,
    Source,
    TestMode,
    User,
    UserCategory,
)
from app.schemas import IntervalOverride, SubscriptionFilters, TimedMode

router = APIRouter(dependencies=[Auth])


def require_subscription(db, uid, kind, sub_id):
    user = require_user(db, uid)
    table = {"category": UserCategory, "search": CustomCategory}.get(kind)
    if table is None:
        raise HTTPException(422, "Unknown subscription kind")
    subscription = db.scalar(select(table).where(table.id == sub_id, table.user_id == user.id))
    if subscription is None:
        raise HTTPException(404, "Subscription not found")
    return subscription


@router.get("/cities")
def cities(db: Db):
    return [{"id": c.id, "name": c.name} for c in db.scalars(select(City).order_by(City.name))]


@router.patch("/users/{uid}/subscriptions/{kind}/{sub_id}/filters")
def update_filters(uid: int, kind: str, sub_id: int, body: SubscriptionFilters, db: Db):
    subscription = require_subscription(db, uid, kind, sub_id)
    available = {c.casefold() for c in db.scalars(select(City.name))}
    if any(c not in available for c in body.cities):
        raise HTTPException(422, "Выберите города из списка магазина")
    subscription.filters = body.model_dump(mode="json")
    db.commit()
    return {"filters": subscription.filters}


@router.get("/users/{uid}/status")
def user_status(uid: int, db: Db):
    user = require_user(db, uid)
    crawler = db.get(CrawlerState, 1)
    subs = [
        *db.scalars(select(UserCategory).where(UserCategory.user_id == user.id)),
        *db.scalars(select(CustomCategory).where(CustomCategory.user_id == user.id)),
    ]
    result = []
    for sub in subs:
        source = db.get(Source, sub.source_id)
        ready = source.generation >= sub.baseline_generation
        unavailable = source.last_error or (
            crawler.blocked_until and crawler.blocked_until > utcnow()
        )
        local_limit = source.last_error in ("Hourly request budget", "Request backoff")
        reason = (
            "paused"
            if not user.active
            else "rate_limited"
            if local_limit
            else "site_unavailable"
            if unavailable
            else "initializing"
            if not ready
            else "scanning"
            if source.scan_started_at
            else "new_items"
            if sub.last_matched_count
            else "no_new_items"
        )
        name = db.get(Category, sub.category_id).name if isinstance(sub, UserCategory) else sub.name
        result.append(
            {
                "name": name,
                "kind": source.kind,
                "reason": reason,
                "last_checked_at": source.last_success_at,
                "next_checked_at": source.next_scan_at,
                "checked": source.item_count
                if source.scan_started_at
                else source.last_checked_count,
                "new_matching": sub.last_matched_count,
                "error": source.last_error,
                "filters": sub.filters,
            }
        )
    return {
        "active": user.active,
        "interval_seconds": scan_interval(db),
        "subscriptions": result,
        "deliveries": dict(
            db.execute(
                select(Notification.status, func.count())
                .where(Notification.user_id == user.id)
                .group_by(Notification.status)
            ).all()
        ),
    }


@router.get("/admin/{uid}/status")
def admin_status(uid: int, db: Db):
    require_admin(uid)
    control = db.get(RuntimeControl, 1)
    now = utcnow()
    crawler = db.get(CrawlerState, 1)
    return {
        "users": db.scalar(select(func.count()).select_from(User)),
        "interval_seconds": scan_interval(db),
        "override_until": control.expires_at,
        "heartbeat_age": (now - crawler.heartbeat_at).total_seconds()
        if crawler.heartbeat_at
        else None,
        "queue": dict(
            db.execute(
                select(Notification.status, func.count()).group_by(Notification.status)
            ).all()
        ),
        "source_errors": [
            {"id": s.id, "kind": s.kind, "error": s.last_error}
            for s in db.scalars(select(Source).where(Source.last_error.is_not(None)).limit(10))
        ],
        "alerts": [
            a.message for a in db.scalars(select(AdminAlert).where(AdminAlert.active.is_(True)))
        ],
        "backup": backup_status(),
        "test_modes": [
            {"user_id": t.user_id, "expires_at": t.expires_at}
            for t in db.scalars(select(TestMode).where(TestMode.expires_at > now))
        ],
    }


@router.get("/admin/{uid}/deliveries")
def admin_deliveries(
    uid: int,
    db: Db,
    status: str = Query("uncertain", pattern="^(uncertain|failed|pending|sending|sent)$"),
):
    require_admin(uid)
    return [
        {
            "id": n.id,
            "status": n.status,
            "attempts": n.attempts,
            "error": n.last_error,
            "created_at": n.created_at,
        }
        for n in db.scalars(
            select(Notification)
            .where(Notification.status == status)
            .order_by(Notification.id.desc())
            .limit(20)
        )
    ]


@router.post("/admin/{uid}/interval")
def temporary_interval(uid: int, body: IntervalOverride, db: Db):
    require_admin(uid)
    control = set_interval(db, body.seconds, body.minutes)
    db.commit()
    return {"seconds": control.interval_seconds, "expires_at": control.expires_at}


@router.delete("/admin/{uid}/interval")
def reset_interval(uid: int, db: Db):
    require_admin(uid)
    control = db.get(RuntimeControl, 1, with_for_update=True)
    control.interval_seconds = control.expires_at = None
    reschedule(db, settings().scan_interval_seconds)
    db.commit()
    return {"seconds": settings().scan_interval_seconds}


@router.post("/admin/{uid}/test")
def test_mode(uid: int, body: TimedMode, db: Db):
    require_admin(uid)
    user = require_user(db, uid)
    until = utcnow() + timedelta(minutes=body.minutes)
    db.execute(
        insert(TestMode)
        .values(user_id=user.id, expires_at=until, next_run_at=utcnow())
        .on_conflict_do_update(
            index_elements=[TestMode.user_id], set_={"expires_at": until, "next_run_at": utcnow()}
        )
    )
    db.commit()
    return {"expires_at": until, "interval_seconds": 300}


@router.delete("/admin/{uid}/test")
def stop_test_mode(uid: int, db: Db):
    require_admin(uid)
    user = require_user(db, uid)
    mode = db.get(TestMode, user.id)
    if mode:
        db.delete(mode)
    db.commit()
    return {"stopped": True}


@router.post("/test-runs/claim")
def claim_test(db: Db):
    mode = db.scalar(
        select(TestMode)
        .join(User)
        .where(
            TestMode.expires_at > utcnow(),
            TestMode.next_run_at <= utcnow(),
            User.telegram_id.in_(settings().admin_ids),
        )
        .order_by(TestMode.next_run_at)
        .with_for_update(skip_locked=True, of=TestMode)
        .limit(1)
    )
    if mode is None:
        return Response(status_code=204)
    mode.next_run_at = utcnow() + timedelta(minutes=5)
    user = db.get(User, mode.user_id)
    result = service.preview_snapshot(db, user, settings().snapshot_limit)
    db.commit()
    return {"chat_id": user.chat_id, "expires_at": mode.expires_at, **result}


@router.post("/admin/{uid}/backup")
def request_backup(uid: int, db: Db):
    require_admin(uid)
    directory = Path(settings().backups_dir)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "request").touch(mode=0o600)
    return {"queued": True}


@router.post("/admin-alerts/claim")
def claim_alerts(db: Db):
    if not settings().admin_ids:
        return []
    now = utcnow()
    alerts = list(
        db.scalars(
            select(AdminAlert)
            .where(
                AdminAlert.active.is_(True),
                (AdminAlert.notified_at.is_(None))
                | (AdminAlert.notified_at < now - timedelta(hours=6)),
            )
            .with_for_update(skip_locked=True)
        )
    )
    for item in alerts:
        item.notified_at = now
    db.commit()
    return [{"chat_id": uid, "message": a.message} for a in alerts for uid in settings().admin_ids]

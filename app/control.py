import json
import shutil
from datetime import timedelta
from pathlib import Path

from sqlalchemy import case, select
from sqlalchemy.dialects.postgresql import insert

from app.config import settings
from app.db import utcnow
from app.models import AdminAlert, RuntimeControl, Source, TestMode


def scan_interval(db) -> int:
    control = db.get(RuntimeControl, 1)
    if (
        control
        and control.interval_seconds
        and control.expires_at
        and control.expires_at > utcnow()
    ):
        return control.interval_seconds
    return settings().scan_interval_seconds


def reschedule(db, seconds: int):
    now = utcnow()
    for source in db.scalars(
        select(Source)
        .where(
            Source.last_error.is_(None),
            Source.scan_started_at.is_(None),
        )
        .with_for_update()
    ):
        if source.last_success_at:
            source.next_scan_at = max(now, source.last_success_at + timedelta(seconds=seconds))


def set_interval(db, seconds: int, minutes: int):
    control = db.get(RuntimeControl, 1, with_for_update=True)
    control.interval_seconds = seconds
    control.expires_at = utcnow() + timedelta(minutes=minutes)
    reschedule(db, seconds)
    alert(db, "interval_expired", "", False)
    return control


def alert(db, key: str, message: str, active: bool):
    db.execute(
        insert(AdminAlert)
        .values(key=key, message=message, active=active)
        .on_conflict_do_update(
            index_elements=[AdminAlert.key],
            set_={
                "message": message,
                "active": active,
                "updated_at": utcnow(),
                "notified_at": case(
                    (AdminAlert.active.is_(False) & active, None), else_=AdminAlert.notified_at
                ),
            },
        )
    )


def backup_status() -> dict:
    path = Path(settings().backups_dir) / "status.json"
    try:
        result = json.loads(path.read_text())
        return result if isinstance(result, dict) else {"result": "not_run", "verified": False}
    except (OSError, ValueError):
        return {"result": "not_run", "verified": False}


def maintenance(db):
    now = utcnow()
    control = db.get(RuntimeControl, 1, with_for_update=True)
    if control and control.expires_at and control.expires_at <= now:
        control.interval_seconds, control.expires_at = None, None
        reschedule(db, settings().scan_interval_seconds)
        alert(
            db,
            "interval_expired",
            "Временный интервал завершён. Восстановлен обычный интервал "
            f"{settings().scan_interval_seconds // 60} мин.",
            True,
        )
    for test in db.scalars(select(TestMode).where(TestMode.expires_at <= now)):
        db.delete(test)
        alert(
            db, f"test_expired:{test.user_id}", "Тестовая рассылка автоматически отключена.", True
        )
    free = shutil.disk_usage(settings().host_disk_path).free
    alert(
        db,
        "disk_low",
        f"Мало свободного места на диске: {free // (1024**2)} МБ. "
        "Освободите место, чтобы Docker и база продолжали работать.",
        free < settings().disk_warn_free_mb * 1024**2,
    )
    status = backup_status()
    alert(
        db,
        "backup_failed",
        "Резервная копия не прошла создание или проверку восстановления. "
        "Откройте /admin для подробностей.",
        status.get("result") == "failed",
    )
    if status.get("last_success_at"):
        from datetime import datetime

        last = datetime.fromisoformat(status["last_success_at"].replace("Z", "+00:00"))
        alert(
            db,
            "backup_stale",
            "Последняя проверенная резервная копия старше 26 часов.",
            last + timedelta(hours=26) < now,
        )
    directory = Path(settings().backups_dir)
    missing = (
        status.get("result") == "not_run"
        and directory.exists()
        and (directory.stat().st_mtime + 600 < now.timestamp())
    )
    alert(
        db,
        "backup_missing",
        "Первая резервная копия ещё не создана. Проверьте сервис backups.",
        missing,
    )

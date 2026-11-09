import secrets
from typing import Annotated

from fastapi import Depends, HTTPException
from fastapi.security import APIKeyHeader
from sqlalchemy.orm import Session

from app.config import settings
from app.db import session_dependency
from app.models import User
from app.service import user_by_telegram

Db = Annotated[Session, Depends(session_dependency)]
key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


def authenticate(key: Annotated[str | None, Depends(key_header)]):
    if not key or not secrets.compare_digest(key, settings().api_key.get_secret_value()):
        raise HTTPException(401, "Invalid API key")


Auth = Depends(authenticate)


def require_user(db: Session, telegram_id: int) -> User:
    user = user_by_telegram(db, telegram_id)
    if user is None:
        raise HTTPException(404, "User not found; register with /start")
    return user


def require_admin(telegram_id: int):
    if telegram_id not in settings().admin_ids:
        raise HTTPException(403, "Admin access required")

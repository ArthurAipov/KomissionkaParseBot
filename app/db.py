from datetime import UTC, datetime
from functools import lru_cache

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from app.config import settings


def utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


@lru_cache
def engine():
    return create_engine(settings().database_url, pool_pre_ping=True, pool_size=5, max_overflow=2)


@lru_cache
def sessions():
    return sessionmaker(engine(), expire_on_commit=False)


def session_dependency():
    with sessions()() as session:
        yield session

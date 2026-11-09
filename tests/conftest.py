import os
from pathlib import Path

import pytest
from sqlalchemy import create_engine, delete
from sqlalchemy.orm import sessionmaker

os.environ.setdefault("API_KEY", "local-tests-api-secret-at-least-32-characters")


@pytest.fixture
def fixture_text():
    return lambda name: (Path(__file__).parent / "fixtures" / name).read_text()


@pytest.fixture
def session_factory():
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        pytest.skip("Set TEST_DATABASE_URL to run PostgreSQL integration tests")
    # Never drop/recreate tables: each test is rolled back in a dedicated test database.
    engine = create_engine(url)
    if "test" not in (engine.url.database or "").lower():
        raise RuntimeError("TEST_DATABASE_URL must point at a database with 'test' in its name")
    with engine.connect() as connection:
        transaction = connection.begin()
        factory = sessionmaker(
            connection, expire_on_commit=False, join_transaction_mode="create_savepoint"
        )
        from app.db import Base, utcnow
        from app.models import CrawlerState

        # Reset data inside the outer rollback, including leftovers from optional live checks.
        # The committed database contents are preserved after the test.
        with factory() as db, db.begin():
            for table in reversed(Base.metadata.sorted_tables):
                if table.name not in ("crawler_state", "runtime_control"):
                    db.execute(delete(table))
            state = db.get(CrawlerState, 1)
            state.next_request_at = state.window_started_at = utcnow()
            state.window_requests = 0
            state.blocked_until = state.block_reason = None
        yield factory
        transaction.rollback()
    engine.dispose()


@pytest.fixture
def db(session_factory):
    with session_factory() as session:
        yield session

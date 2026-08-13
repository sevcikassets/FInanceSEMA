"""Shared pytest fixtures.

Tests that need a real database are marked with `requires_db` and skip
automatically when no PostgreSQL instance is reachable (e.g. when running
`pytest` outside of `docker compose`). Point `TEST_DATABASE_URL` at a
disposable database to run them; otherwise the local dev `db` service from
`docker-compose.yml` is used by default.
"""

from __future__ import annotations

import os

os.environ.setdefault(
    "DATABASE_URL",
    os.environ.get("TEST_DATABASE_URL", "postgresql+psycopg://finance:finance@localhost:5432/finance_sema"),
)
os.environ.setdefault("APP_USERNAME", "admin")
os.environ.setdefault("APP_PASSWORD", "finance")
os.environ.setdefault("APP_TOKEN_SECRET", "test-secret")

import pytest  # noqa: E402


def _database_available() -> bool:
    try:
        from sqlalchemy import create_engine

        engine = create_engine(os.environ["DATABASE_URL"])
        with engine.connect():
            pass
        engine.dispose()
        return True
    except Exception:
        return False


requires_db = pytest.mark.skipif(
    not _database_available(),
    reason="Vyžaduje dostupnou PostgreSQL databázi (DATABASE_URL/TEST_DATABASE_URL), např. `docker compose up -d db`.",
)


@pytest.fixture()
def db_session():
    from app.db import Base, SessionLocal, engine

    # Ensure a clean slate even if a previous run (or manual script) left data
    # behind - tests must not depend on execution order.
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    session = SessionLocal()
    try:
        yield session
    finally:
        session.rollback()
        session.close()
        Base.metadata.drop_all(bind=engine)


@pytest.fixture()
def client(db_session):
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as test_client:
        yield test_client

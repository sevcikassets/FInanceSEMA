"""Shared pytest fixtures.

Tests that need a real database are marked with `requires_db` and skip
automatically when no PostgreSQL instance is reachable (e.g. when running
`pytest` outside of `docker compose`). Point `TEST_DATABASE_URL` at a
disposable database to run them.

IMPORTANT - this suite drops and recreates every table before AND after each
test (see db_session below). Running it against the shared dev/prod database
destroys real data - this has actually happened twice during development
(`docker compose exec api pytest` inherits the api container's own
DATABASE_URL, which points at the real dev `db` service, and TEST_DATABASE_URL
is only consulted as a *default* when DATABASE_URL is unset - so it silently
wiped the dev database both times). _require_test_database() below refuses to
run any DB-touching test unless the resolved database name looks like a
dedicated test database, specifically to stop that from happening a third
time. To run these tests safely:

    docker compose exec db createdb -U finance finance_sema_test  # once
    docker compose exec -e TEST_DATABASE_URL=postgresql+psycopg://finance:finance@db:5432/finance_sema_test api pytest
"""

from __future__ import annotations

import os

if os.environ.get("TEST_DATABASE_URL"):
    # Explicit TEST_DATABASE_URL always wins, even if DATABASE_URL is
    # already set (e.g. inherited from the api container's own environment,
    # which points at the real dev database) - setdefault() alone silently
    # ignores TEST_DATABASE_URL whenever DATABASE_URL happens to be
    # pre-populated, which is exactly the situation that caused this suite
    # to wipe the shared dev database more than once (see the module
    # docstring above).
    os.environ["DATABASE_URL"] = os.environ["TEST_DATABASE_URL"]
else:
    os.environ.setdefault("DATABASE_URL", "postgresql+psycopg://finance:finance@localhost:5432/finance_sema_test")
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


def _require_test_database() -> None:
    """Hard safety check, not just a naming convention: db_session drops
    every table twice per test, so accidentally pointing it at the real dev
    or production database is a full data-loss incident, not a bug - this
    has already happened. Only proceeds if the database name in
    DATABASE_URL contains "test"; everything else raises instead of
    silently wiping whatever it's pointed at."""
    from sqlalchemy.engine import make_url

    database_name = make_url(os.environ["DATABASE_URL"]).database or ""
    if "test" not in database_name.lower():
        raise RuntimeError(
            f"Refusing to run DB-touching tests against database {database_name!r} - "
            "this fixture drops every table before and after each test. Point "
            "TEST_DATABASE_URL at a database whose name contains 'test' (e.g. "
            "finance_sema_test), see the module docstring in conftest.py."
        )


@pytest.fixture()
def db_session():
    _require_test_database()
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
def portfolio_id(db_session):
    from app.models import Portfolio

    portfolio = Portfolio(name="Test Subjekt")
    db_session.add(portfolio)
    db_session.commit()
    return portfolio.id


@pytest.fixture()
def client(db_session):
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as test_client:
        yield test_client

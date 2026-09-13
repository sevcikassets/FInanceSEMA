from collections.abc import Generator
from uuid import UUID

from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from .config import get_settings

# Read-only connection to the Activities app's own database - a separate
# docker-compose project on the same host, reachable only via the
# shared_infra network (see docker-compose.yml). No Base/models here on
# purpose: this app never migrates or writes to Activities' schema, so a
# full ORM mapping would be misleading upkeep - the handful of columns
# actually needed are queried directly by the helpers below instead.
# connect_timeout bounds how long a connection attempt can hang if
# Activities/shared_infra is down or misconfigured - without it, a broken
# network path can leave the TCP handshake hanging far longer than any
# reasonable page load (observed: multiple minutes), which defeats the
# graceful-degradation handling in main.py's /activities/projects (that
# only helps once an exception is actually raised).
activities_engine = create_engine(
    get_settings().activities_database_url, pool_pre_ping=True, connect_args={"connect_timeout": 3}
)
ActivitiesSessionLocal = sessionmaker(bind=activities_engine, autoflush=False, autocommit=False)


def get_activities_db() -> Generator[Session, None, None]:
    db = ActivitiesSessionLocal()
    try:
        yield db
    finally:
        db.close()


def list_activities_projects(db: Session) -> list[dict]:
    """{id, name} for every active Activities project - powers both the
    "Projekt v Activities" picker on an Asset and, client-side, resolving a
    linked activities_project_id to its display name."""
    rows = db.execute(text("SELECT id, name FROM projects WHERE is_active ORDER BY name")).all()
    return [{"id": str(row.id), "name": row.name} for row in rows]


def list_activities_time_entries(db: Session, project_id: UUID) -> list[dict]:
    """Work log for one Activities project, most recent first - the
    "Odpracované práce" list on an Asset's Majetek card."""
    rows = db.execute(
        text(
            "SELECT spent_on, description, duration_hours, category_code "
            "FROM time_entries WHERE project_id = :project_id ORDER BY spent_on DESC"
        ),
        {"project_id": project_id},
    ).all()
    return [
        {
            "spent_on": row.spent_on.isoformat() if row.spent_on else None,
            "description": row.description,
            "duration_hours": float(row.duration_hours) if row.duration_hours is not None else None,
            "category_code": row.category_code,
        }
        for row in rows
    ]

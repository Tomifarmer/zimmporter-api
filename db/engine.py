"""Database engine and session management.

Creates a SQLAlchemy sync engine against MariaDB using credentials from
environment variables.  Provides a :func:`get_session` context manager
that auto-commits on success and auto-rolls back on exception.

Environment variables (with defaults):

* ``DB_HOST`` — MariaDB host (default ``localhost``)
* ``DB_PORT`` — MariaDB port (default ``3306``)
* ``DB_USER`` — Username (default ``root``)
* ``DB_PASS`` — Password (default ``root``)
* ``DB_NAME`` — Database name (default ``zimmporter``)
"""

import os
from contextlib import contextmanager

from sqlalchemy import create_engine
from sqlalchemy.orm import scoped_session, sessionmaker

from db.models import Base

db_host = os.getenv("DB_HOST", "localhost")
db_port = os.getenv("DB_PORT", "3306")
db_user = os.getenv("DB_USER", "root")
db_pass = os.getenv("DB_PASS", "root")
db_name = os.getenv("DB_NAME", "zimmporter")

url = f"mysql+pymysql://{db_user}:{db_pass}@{db_host}:{db_port}/{db_name}"

engine = create_engine(url, pool_pre_ping=True)
session_factory = sessionmaker(bind=engine)
ScopedSession = scoped_session(session_factory)


def init_db() -> None:
    """Create all tables defined in :data:`db.models.Base` metadata if they don't exist."""
    Base.metadata.create_all(engine)
    migrate_schema()


def migrate_schema() -> None:
    """Add missing columns that were introduced after the initial table creation."""
    from sqlalchemy import inspect
    from sqlalchemy import text as sa_text

    inspector = inspect(engine)

    song_additions = {
        "release_date": "ALTER TABLE songs ADD COLUMN release_date DATE NULL",
        "s3_path": "ALTER TABLE songs ADD COLUMN s3_path VARCHAR(1024) NULL",
    }

    existing_song_columns = {col["name"] for col in inspector.get_columns("songs")}
    with engine.connect() as conn:
        for col, ddl in song_additions.items():
            if col not in existing_song_columns:
                conn.execute(sa_text(ddl))
        conn.commit()

    status_col = next(
        (col for col in inspector.get_columns("songs") if col["name"] == "status"), None
    )
    if status_col is not None and hasattr(status_col["type"], "enums"):
        current_enums = list(status_col["type"].enums)
        if "unavailable" not in current_enums:
            enum_values = ",".join(f"'{v}'" for v in current_enums + ["unavailable"])
            with engine.connect() as conn:
                conn.execute(sa_text(f"ALTER TABLE songs MODIFY COLUMN status ENUM({enum_values}) NOT NULL"))
                conn.commit()

    job_additions = {
        "requested_by": "ALTER TABLE jobs ADD COLUMN requested_by VARCHAR(256) NULL",
        "requested_groups": "ALTER TABLE jobs ADD COLUMN requested_groups VARCHAR(512) NULL",
    }

    existing_job_columns = {col["name"] for col in inspector.get_columns("jobs")}
    with engine.connect() as conn:
        for col, ddl in job_additions.items():
            if col not in existing_job_columns:
                conn.execute(sa_text(ddl))
        conn.commit()


@contextmanager
def get_session():
    """Context manager that yields a scoped database session.

    Commits on successful exit, rolls back and re-raises on exception,
    and always removes the scoped session in the finally block.

    Example:
        >>> with get_session() as session:
        ...     session.add(MyModel())
    """
    session = ScopedSession()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        ScopedSession.remove()

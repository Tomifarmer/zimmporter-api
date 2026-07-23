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

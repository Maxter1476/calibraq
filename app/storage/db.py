"""SQLAlchemy engine and session helpers.

The database path resolves in this order: explicit argument, the
``CALIBRAQ_DB_PATH`` environment variable, then ``data/calibraq.db`` relative
to the current working directory.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.storage.tables import Base

DEFAULT_DB_PATH = Path("data") / "calibraq.db"


def resolve_db_path(db_path: str | Path | None = None) -> Path:
    """Resolve the SQLite database path (argument > env var > default)."""
    if db_path is not None:
        return Path(db_path)
    env_path = os.getenv("CALIBRAQ_DB_PATH")
    if env_path:
        return Path(env_path)
    return DEFAULT_DB_PATH


def get_engine(db_path: str | Path | None = None) -> Engine:
    """Create a SQLite engine, creating the parent directory if needed."""
    path = resolve_db_path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    return create_engine(f"sqlite:///{path}")


def init_db(engine: Engine) -> None:
    """Create all CalibraQ tables if they do not exist."""
    Base.metadata.create_all(engine)


@contextmanager
def session_scope(engine: Engine) -> Iterator[Session]:
    """Provide a transactional session: commit on success, rollback on error."""
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()

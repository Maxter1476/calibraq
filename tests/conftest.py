"""Shared pytest fixtures."""

from __future__ import annotations

import sys
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.storage.db import get_engine, init_db


@pytest.fixture()
def engine(tmp_path: Path) -> Engine:
    """A fresh SQLite database per test."""
    eng = get_engine(tmp_path / "test.db")
    init_db(eng)
    return eng


@pytest.fixture()
def session(engine: Engine) -> Iterator[Session]:
    """A session bound to the per-test database."""
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    sess = factory()
    try:
        yield sess
        sess.commit()
    finally:
        sess.close()

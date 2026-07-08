"""Persist snapshots: raw JSON file per (backend, poll) plus SQLite insert."""

from __future__ import annotations

from pathlib import Path

from sqlalchemy.orm import Session

from app.calibration.loaders import store_snapshot
from app.calibration.models import CalibrationSnapshot

DEFAULT_SNAPSHOT_DIR = Path("data") / "snapshots"


def write_raw_snapshot(snapshot: CalibrationSnapshot, snapshot_dir: str | Path) -> Path:
    """Write the raw snapshot JSON to ``snapshot_dir``; return the file path.

    One file per backend per poll, named by backend and calibration timestamp.
    Existing files are left untouched (idempotent on re-collection).
    """
    directory = Path(snapshot_dir)
    directory.mkdir(parents=True, exist_ok=True)
    stamp = snapshot.timestamp.strftime("%Y%m%dT%H%M%SZ")
    path = directory / f"{snapshot.backend_name}_{stamp}.json"
    if not path.exists():
        path.write_text(snapshot.model_dump_json(indent=2))
    return path


def persist_snapshot(
    session: Session, snapshot: CalibrationSnapshot, snapshot_dir: str | Path
) -> tuple[Path, bool]:
    """Write raw JSON and insert into SQLite; return ``(json_path, inserted)``."""
    path = write_raw_snapshot(snapshot, snapshot_dir)
    _, inserted = store_snapshot(session, snapshot, raw_json_path=str(path))
    return path, inserted

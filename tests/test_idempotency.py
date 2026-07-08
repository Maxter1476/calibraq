"""Collector insert path must be idempotent on (backend, calibration timestamp)."""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.calibration.loaders import store_snapshot
from app.collector.mock import MOCK_BACKEND_SPECS, generate_history
from app.collector.persist import persist_snapshot
from app.storage.tables import CalibrationSnapshotRow, EdgeMetricRow, QubitMetricRow


def _row_counts(session: Session) -> tuple[int, int, int]:
    """Counts of (snapshots, qubit metric rows, edge metric rows)."""
    return (
        session.scalar(select(func.count(CalibrationSnapshotRow.id))) or 0,
        session.scalar(select(func.count(QubitMetricRow.id))) or 0,
        session.scalar(select(func.count(EdgeMetricRow.id))) or 0,
    )


def test_store_snapshot_idempotent(session: Session) -> None:
    """Storing the same mock history twice inserts nothing the second time."""
    spec = MOCK_BACKEND_SPECS[0]
    snapshots = generate_history(spec, days=3, polls_per_day=2, seed=42)

    first_pass = [store_snapshot(session, snap) for snap in snapshots]
    session.commit()
    assert all(inserted for _, inserted in first_pass)
    counts_after_first = _row_counts(session)

    second_pass = [store_snapshot(session, snap) for snap in snapshots]
    session.commit()
    assert not any(inserted for _, inserted in second_pass)
    assert _row_counts(session) == counts_after_first

    # The same snapshot ids are reported on the second pass.
    assert [sid for sid, _ in first_pass] == [sid for sid, _ in second_pass]


def test_persist_snapshot_idempotent(session: Session, tmp_path: Path) -> None:
    """persist_snapshot (raw JSON + insert) is also safe to re-run."""
    spec = MOCK_BACKEND_SPECS[1]
    snapshot = generate_history(spec, days=1, polls_per_day=1, seed=7)[0]

    path1, inserted1 = persist_snapshot(session, snapshot, tmp_path)
    session.commit()
    path2, inserted2 = persist_snapshot(session, snapshot, tmp_path)
    session.commit()

    assert inserted1 and not inserted2
    assert path1 == path2
    assert len(list(tmp_path.glob("*.json"))) == 1

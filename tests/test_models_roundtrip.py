"""Snapshot round-trip: JSON -> Pydantic -> ORM -> Pydantic must be lossless."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.calibration.loaders import load_snapshots, store_snapshot
from app.calibration.models import CalibrationSnapshot, EdgeMetrics, QubitMetrics


def _build_snapshot() -> CalibrationSnapshot:
    """A snapshot exercising full metrics, missing metrics, and outages."""
    ts = datetime(2026, 6, 10, 8, 30, tzinfo=timezone.utc)
    cal_a = datetime(2026, 6, 10, 6, 0, tzinfo=timezone.utc)
    cal_b = datetime(2026, 6, 10, 6, 17, tzinfo=timezone.utc)
    return CalibrationSnapshot(
        backend_name="unit_test_backend",
        timestamp=ts,
        num_qubits=2,
        coupling_map=[(0, 1), (1, 0)],
        qubits=[
            QubitMetrics(
                qubit_index=0,
                t1_us=143.2,
                t1_calibrated_at=cal_a,
                t2_us=88.1,
                t2_calibrated_at=cal_a,
                frequency_ghz=4.972,
                frequency_calibrated_at=cal_a,
                anharmonicity_ghz=-0.331,
                anharmonicity_calibrated_at=cal_a,
                readout_error=0.012,
                readout_error_calibrated_at=cal_b,
                sx_gate_error=2.4e-4,
                sx_gate_error_calibrated_at=cal_b,
                operational=True,
                operational_calibrated_at=ts,
            ),
            # Missing metrics + non-operational must round-trip too.
            QubitMetrics(qubit_index=1, t1_us=None, readout_error=0.5, operational=False),
        ],
        edges=[
            EdgeMetrics(
                q0=0,
                q1=1,
                gate_name="cz",
                two_qubit_gate_error=7.3e-3,
                two_qubit_gate_error_calibrated_at=cal_a,
                gate_duration_ns=68.0,
                gate_duration_calibrated_at=cal_a,
            ),
            EdgeMetrics(q0=1, q1=0, gate_name="cz", two_qubit_gate_error=None),
        ],
        snapshot_source="ibm",
    )


def test_json_pydantic_orm_roundtrip(session: Session) -> None:
    """JSON -> Pydantic -> ORM -> Pydantic yields an identical model."""
    original = _build_snapshot()

    # JSON -> Pydantic
    reparsed = CalibrationSnapshot.model_validate_json(original.model_dump_json())
    assert reparsed == original

    # Pydantic -> ORM -> Pydantic
    snapshot_id, inserted = store_snapshot(session, reparsed)
    assert inserted and snapshot_id is not None
    session.commit()

    loaded = load_snapshots(session, backend_name="unit_test_backend")
    assert len(loaded) == 1
    assert loaded[0] == original


def test_naive_datetimes_normalized_to_utc() -> None:
    """Naive datetimes (the SQLite convention) come back tz-aware UTC."""
    qubit = QubitMetrics(qubit_index=0, t1_calibrated_at=datetime(2026, 6, 10, 6, 0))
    assert qubit.t1_calibrated_at is not None
    assert qubit.t1_calibrated_at.tzinfo == timezone.utc

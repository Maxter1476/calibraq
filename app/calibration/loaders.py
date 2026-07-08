"""Loaders between the Pydantic calibration model and the ORM tables.

The storage idempotency key is ``(backend, snapshot.timestamp)`` — for IBM
data the timestamp is the calibration timestamp (``last_update_date``), so
re-running the collector between recalibrations inserts nothing.
"""

from __future__ import annotations

import json

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.calibration.models import CalibrationSnapshot, EdgeMetrics, QubitMetrics
from app.storage.tables import Backend, CalibrationSnapshotRow, EdgeMetricRow, QubitMetricRow
from app.utils.timeutils import to_utc_naive, utcnow


def upsert_backend(session: Session, name: str, num_qubits: int) -> Backend:
    """Return the backend row for ``name``, creating it if unseen."""
    backend = session.scalar(select(Backend).where(Backend.name == name))
    if backend is None:
        backend = Backend(name=name, num_qubits=num_qubits)
        session.add(backend)
        session.flush()
    else:
        backend.num_qubits = num_qubits
        last_seen = to_utc_naive(utcnow())
        assert last_seen is not None
        backend.last_seen_at = last_seen
    return backend


def store_snapshot(
    session: Session, snapshot: CalibrationSnapshot, raw_json_path: str | None = None
) -> tuple[int, bool]:
    """Insert a snapshot if not already stored; return ``(snapshot_id, inserted)``.

    Idempotent on ``(backend, snapshot.timestamp)``: if a row already exists
    for that key, nothing is written and ``inserted`` is ``False``.
    """
    backend = upsert_backend(session, snapshot.backend_name, snapshot.num_qubits)
    cal_ts = to_utc_naive(snapshot.timestamp)
    existing = session.scalar(
        select(CalibrationSnapshotRow).where(
            CalibrationSnapshotRow.backend_id == backend.id,
            CalibrationSnapshotRow.snapshot_timestamp == cal_ts,
        )
    )
    if existing is not None:
        return existing.id, False

    row = CalibrationSnapshotRow(
        backend_id=backend.id,
        snapshot_timestamp=cal_ts,
        num_qubits=snapshot.num_qubits,
        coupling_map_json=json.dumps([list(edge) for edge in snapshot.coupling_map]),
        snapshot_source=snapshot.snapshot_source,
        raw_json_path=raw_json_path,
    )
    row.qubit_metrics = [
        QubitMetricRow(
            qubit_index=q.qubit_index,
            t1_us=q.t1_us,
            t1_calibrated_at=to_utc_naive(q.t1_calibrated_at),
            t2_us=q.t2_us,
            t2_calibrated_at=to_utc_naive(q.t2_calibrated_at),
            frequency_ghz=q.frequency_ghz,
            frequency_calibrated_at=to_utc_naive(q.frequency_calibrated_at),
            anharmonicity_ghz=q.anharmonicity_ghz,
            anharmonicity_calibrated_at=to_utc_naive(q.anharmonicity_calibrated_at),
            readout_error=q.readout_error,
            readout_error_calibrated_at=to_utc_naive(q.readout_error_calibrated_at),
            sx_gate_error=q.sx_gate_error,
            sx_gate_error_calibrated_at=to_utc_naive(q.sx_gate_error_calibrated_at),
            operational=q.operational,
            operational_calibrated_at=to_utc_naive(q.operational_calibrated_at),
        )
        for q in snapshot.qubits
    ]
    row.edge_metrics = [
        EdgeMetricRow(
            q0=e.q0,
            q1=e.q1,
            gate_name=e.gate_name,
            two_qubit_gate_error=e.two_qubit_gate_error,
            two_qubit_gate_error_calibrated_at=to_utc_naive(e.two_qubit_gate_error_calibrated_at),
            gate_duration_ns=e.gate_duration_ns,
            gate_duration_calibrated_at=to_utc_naive(e.gate_duration_calibrated_at),
        )
        for e in snapshot.edges
    ]
    session.add(row)
    session.flush()
    return row.id, True


def row_to_snapshot(row: CalibrationSnapshotRow) -> CalibrationSnapshot:
    """Rebuild a :class:`CalibrationSnapshot` from its ORM row.

    Stored datetimes are UTC tz-naive; the Pydantic validators re-attach UTC.
    """
    return CalibrationSnapshot(
        backend_name=row.backend.name,
        timestamp=row.snapshot_timestamp,
        num_qubits=row.num_qubits,
        coupling_map=[tuple(edge) for edge in json.loads(row.coupling_map_json)],
        qubits=[
            QubitMetrics(
                qubit_index=q.qubit_index,
                t1_us=q.t1_us,
                t1_calibrated_at=q.t1_calibrated_at,
                t2_us=q.t2_us,
                t2_calibrated_at=q.t2_calibrated_at,
                frequency_ghz=q.frequency_ghz,
                frequency_calibrated_at=q.frequency_calibrated_at,
                anharmonicity_ghz=q.anharmonicity_ghz,
                anharmonicity_calibrated_at=q.anharmonicity_calibrated_at,
                readout_error=q.readout_error,
                readout_error_calibrated_at=q.readout_error_calibrated_at,
                sx_gate_error=q.sx_gate_error,
                sx_gate_error_calibrated_at=q.sx_gate_error_calibrated_at,
                operational=q.operational,
                operational_calibrated_at=q.operational_calibrated_at,
            )
            for q in row.qubit_metrics
        ],
        edges=[
            EdgeMetrics(
                q0=e.q0,
                q1=e.q1,
                gate_name=e.gate_name,
                two_qubit_gate_error=e.two_qubit_gate_error,
                two_qubit_gate_error_calibrated_at=e.two_qubit_gate_error_calibrated_at,
                gate_duration_ns=e.gate_duration_ns,
                gate_duration_calibrated_at=e.gate_duration_calibrated_at,
            )
            for e in row.edge_metrics
        ],
        snapshot_source=row.snapshot_source,  # type: ignore[arg-type]
    )


def load_snapshots(session: Session, backend_name: str | None = None) -> list[CalibrationSnapshot]:
    """Load stored snapshots (optionally one backend), ordered by timestamp."""
    stmt = (
        select(CalibrationSnapshotRow)
        .join(Backend)
        .order_by(Backend.name, CalibrationSnapshotRow.snapshot_timestamp)
    )
    if backend_name is not None:
        stmt = stmt.where(Backend.name == backend_name)
    return [row_to_snapshot(row) for row in session.scalars(stmt).all()]

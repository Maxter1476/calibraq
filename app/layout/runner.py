"""Orchestrate drift-aware layout selection over stored snapshots.

Loads a backend's history, computes the phase-2 drift report, builds a
benchmark circuit matching the backend's native two-qubit gate, runs the
drift-aware vs. mapomatic-baseline comparison on the latest snapshot, and
persists both chosen layouts (``layout_recommendations``) and the full
comparison (``analysis_runs`` with ``run_type="layout_validation"``).
"""

from __future__ import annotations

import json

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.analysis.drift import compute_backend_drift
from app.calibration.loaders import load_snapshots
from app.layout.circuits import build_circuit, describe
from app.layout.selection import DEFAULT_RISK_FACTOR
from app.layout.validation import LayoutComparison, compare_layouts
from app.storage.tables import AnalysisRun, Backend, CalibrationSnapshotRow, LayoutRecommendation


def _native_gate(edges_gate_names: list[str]) -> str:
    """Pick the backend's two-qubit gate name (most common), default ``cz``."""
    if not edges_gate_names:
        return "cz"
    return max(set(edges_gate_names), key=edges_gate_names.count)


def _latest_snapshot_id(session: Session, backend_name: str) -> tuple[int, int]:
    """Return ``(backend_id, latest_snapshot_id)`` for a backend."""
    row = session.scalars(
        select(CalibrationSnapshotRow)
        .join(Backend)
        .where(Backend.name == backend_name)
        .order_by(CalibrationSnapshotRow.snapshot_timestamp.desc())
        .limit(1)
    ).first()
    if row is None:
        raise ValueError(f"no snapshots stored for backend {backend_name!r}")
    return row.backend_id, row.id


def run_layout_selection(
    session: Session,
    backend_name: str,
    kind: str = "linear",
    n_qubits: int = 3,
    risk_factor: float = DEFAULT_RISK_FACTOR,
    window_days: float = 14.0,
) -> tuple[LayoutComparison, dict[str, int]]:
    """Select and compare layouts for one backend; persist the results.

    Returns ``(comparison, run_ids)`` where ``run_ids`` maps
    ``{"validation": analysis_run_id, "static": rec_id, "drift_aware": rec_id}``.
    """
    snapshots = load_snapshots(session, backend_name=backend_name)
    if not snapshots:
        raise ValueError(f"no snapshots stored for backend {backend_name!r}")
    latest = snapshots[-1]

    drift_report = compute_backend_drift(snapshots, window_days=window_days)
    gate_name = _native_gate([e.gate_name for e in latest.edges])
    descriptor = describe(kind, n_qubits)
    circuit = build_circuit(kind, n_qubits, gate_name=gate_name)

    comparison = compare_layouts(circuit, descriptor, latest, drift_report, risk_factor=risk_factor)

    backend_id, snapshot_id = _latest_snapshot_id(session, backend_name)
    run = AnalysisRun(
        run_type="layout_validation",
        params_json=json.dumps(
            {
                "backend": backend_name,
                "circuit": descriptor,
                "gate_name": gate_name,
                "risk_factor": risk_factor,
                "window_days": window_days,
            }
        ),
        results_json=comparison.model_dump_json(),
    )
    session.add(run)

    static_rec = LayoutRecommendation(
        backend_id=backend_id,
        snapshot_id=snapshot_id,
        circuit_descriptor=descriptor,
        layout_json=json.dumps(comparison.static_best_layout),
        score=comparison.static_best_cost,
        method="mapomatic_static",
    )
    drift_rec = LayoutRecommendation(
        backend_id=backend_id,
        snapshot_id=snapshot_id,
        circuit_descriptor=descriptor,
        layout_json=json.dumps(comparison.drift_best_layout),
        score=comparison.drift_best_cost,
        method=f"drift_aware_k{risk_factor:g}",
    )
    session.add_all([static_rec, drift_rec])
    session.flush()

    return comparison, {
        "validation": run.id,
        "static": static_rec.id,
        "drift_aware": drift_rec.id,
    }

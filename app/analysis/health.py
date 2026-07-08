"""Qubit and backend health scoring over a time window.

A health score is a 0-100 weighted composite per qubit:

- coherence: median T1 (vs. 150 us reference) and T2 (vs. 100 us)
- fidelity: median readout error and sx gate error on a -log10 scale
  (1e-3 readout -> 1.0, 1e-4 sx -> 1.0)
- uptime: fraction of snapshots where the qubit was operational
- stability: ``1 - drift_score`` from the recalibration-aware drift report

Missing components are dropped and the remaining weights renormalized, so a
qubit with no anharmonicity/sx data is scored on what we know rather than
silently penalized. Backend health combines mean qubit health, the median
two-qubit gate error, and the backend-wide operational fraction.

Results land in ``analysis_runs`` as ``run_type="health"``. Reference scales
are calibration conventions, not validated claims — validation happens in
phase 3 against real data.
"""

from __future__ import annotations

import json
import math
import statistics
from datetime import datetime

import numpy as np
from pydantic import BaseModel, ConfigDict
from sqlalchemy.orm import Session

from app.analysis.drift import BackendDriftReport, compute_backend_drift
from app.analysis.series import filter_window
from app.calibration.loaders import load_snapshots
from app.calibration.models import CalibrationSnapshot
from app.storage.tables import AnalysisRun
from app.utils.timeutils import utcnow

#: Reference scales mapping raw metrics to [0, 1] component scores.
T1_REFERENCE_US = 150.0
T2_REFERENCE_US = 100.0
READOUT_LOG_SCALE = 3.0  # -log10(readout_error) / 3 -> 1.0 at 1e-3
SX_LOG_SCALE = 4.0  # -log10(sx_error) / 4 -> 1.0 at 1e-4
TWO_QUBIT_LOG_SCALE = 3.0  # -log10(2q error) / 3 -> 1.0 at 1e-3

#: Component weights (renormalized over the components actually present).
QUBIT_WEIGHTS: dict[str, float] = {
    "t1": 0.20,
    "t2": 0.10,
    "readout": 0.20,
    "sx": 0.20,
    "uptime": 0.20,
    "stability": 0.10,
}


class QubitHealth(BaseModel):
    """Health summary for one qubit over the window."""

    model_config = ConfigDict(extra="forbid")

    qubit_index: int
    components: dict[str, float]
    median_t1_us: float | None = None
    median_t2_us: float | None = None
    median_readout_error: float | None = None
    median_sx_gate_error: float | None = None
    uptime: float
    drift_score: float
    health_score: float


class BackendHealth(BaseModel):
    """Health summary for one backend over the window."""

    model_config = ConfigDict(extra="forbid")

    backend_name: str
    window_days: float
    generated_at: datetime
    n_snapshots: int
    qubits: list[QubitHealth]
    median_two_qubit_error: float | None = None
    operational_fraction: float
    mean_qubit_health: float
    health_score: float


def _clip01(value: float) -> float:
    """Clamp to [0, 1]."""
    return max(0.0, min(1.0, value))


def _log_error_score(error: float, scale: float) -> float:
    """Map an error rate to [0, 1]: smaller error -> higher score."""
    bounded = min(max(error, 1e-6), 1.0)
    return _clip01(-math.log10(bounded) / scale)


def _median_or_none(values: list[float]) -> float | None:
    """Median of a possibly empty list."""
    return statistics.median(values) if values else None


def _qubit_health(
    snapshots: list[CalibrationSnapshot], qubit_index: int, drift_score: float
) -> QubitHealth:
    """Score one qubit from its windowed observations and drift score."""
    observations = [
        q
        for snap in snapshots
        for q in snap.qubits
        if q.qubit_index == qubit_index
    ]
    t1 = _median_or_none([q.t1_us for q in observations if q.t1_us is not None])
    t2 = _median_or_none([q.t2_us for q in observations if q.t2_us is not None])
    readout = _median_or_none(
        [q.readout_error for q in observations if q.readout_error is not None]
    )
    sx = _median_or_none(
        [q.sx_gate_error for q in observations if q.sx_gate_error is not None]
    )
    uptime = (
        sum(1 for q in observations if q.operational) / len(observations)
        if observations
        else 0.0
    )

    components: dict[str, float] = {"uptime": uptime, "stability": _clip01(1.0 - drift_score)}
    if t1 is not None:
        components["t1"] = _clip01(t1 / T1_REFERENCE_US)
    if t2 is not None:
        components["t2"] = _clip01(t2 / T2_REFERENCE_US)
    if readout is not None:
        components["readout"] = _log_error_score(readout, READOUT_LOG_SCALE)
    if sx is not None:
        components["sx"] = _log_error_score(sx, SX_LOG_SCALE)

    total_weight = sum(QUBIT_WEIGHTS[name] for name in components)
    score = 100.0 * sum(
        QUBIT_WEIGHTS[name] * value for name, value in components.items()
    ) / total_weight

    return QubitHealth(
        qubit_index=qubit_index,
        components={k: round(v, 4) for k, v in components.items()},
        median_t1_us=t1,
        median_t2_us=t2,
        median_readout_error=readout,
        median_sx_gate_error=sx,
        uptime=uptime,
        drift_score=drift_score,
        health_score=round(score, 2),
    )


def compute_backend_health(
    snapshots: list[CalibrationSnapshot],
    drift_report: BackendDriftReport | None = None,
    window_days: float = 14.0,
) -> BackendHealth:
    """Compute the health report for one backend's snapshot history.

    Computes (or reuses) the drift report for the stability component.
    """
    windowed = filter_window(snapshots, window_days)
    if drift_report is None:
        drift_report = compute_backend_drift(snapshots, window_days=window_days)
    drift_by_qubit = {q.qubit_index: q.drift_score for q in drift_report.qubits}

    backend_name = windowed[0].backend_name if windowed else ""
    num_qubits = windowed[0].num_qubits if windowed else 0

    qubit_health = [
        _qubit_health(windowed, index, drift_by_qubit.get(index, 0.0))
        for index in range(num_qubits)
    ]

    two_qubit_errors = [
        e.two_qubit_gate_error
        for snap in windowed
        for e in snap.edges
        if e.two_qubit_gate_error is not None
    ]
    median_2q = _median_or_none(two_qubit_errors)

    all_flags = [q.operational for snap in windowed for q in snap.qubits]
    operational_fraction = sum(all_flags) / len(all_flags) if all_flags else 0.0

    mean_qubit = float(np.mean([q.health_score for q in qubit_health])) if qubit_health else 0.0
    two_qubit_score = (
        _log_error_score(median_2q, TWO_QUBIT_LOG_SCALE) if median_2q is not None else None
    )
    if two_qubit_score is not None:
        backend_score = 100.0 * (
            0.6 * mean_qubit / 100.0 + 0.2 * two_qubit_score + 0.2 * operational_fraction
        )
    else:
        backend_score = 100.0 * (0.75 * mean_qubit / 100.0 + 0.25 * operational_fraction)

    return BackendHealth(
        backend_name=backend_name,
        window_days=window_days,
        generated_at=utcnow(),
        n_snapshots=len(windowed),
        qubits=qubit_health,
        median_two_qubit_error=median_2q,
        operational_fraction=round(operational_fraction, 4),
        mean_qubit_health=round(mean_qubit, 2),
        health_score=round(backend_score, 2),
    )


def run_health_analysis(
    session: Session,
    backend_name: str,
    window_days: float = 14.0,
    drift_report: BackendDriftReport | None = None,
) -> tuple[int, BackendHealth]:
    """Compute health for a stored backend and persist it as an analysis run.

    Returns ``(analysis_run_id, report)``.
    """
    snapshots = load_snapshots(session, backend_name=backend_name)
    if not snapshots:
        raise ValueError(f"no snapshots stored for backend {backend_name!r}")
    report = compute_backend_health(snapshots, drift_report=drift_report, window_days=window_days)
    run = AnalysisRun(
        run_type="health",
        params_json=json.dumps({"backend": backend_name, "window_days": window_days}),
        results_json=report.model_dump_json(),
    )
    session.add(run)
    session.flush()
    return run.id, report

"""Recalibration-aware drift detection per metric, per qubit, per edge.

Recalibration events are detected from changes in the per-metric
``calibrated_at`` timestamp — never inferred from value jumps. Three drift
signals per metric series, all on log scale (metrics are positive and span
decades):

- **volatility**: std of log-changes between consecutive calibration-epoch
  medians — how much a metric moves recalibration to recalibration.
- **trend_per_day**: linear regression slope of log(value) vs. time over the
  whole window — slow secular degradation or improvement (fractional/day).
- **within_epoch_growth**: median fractional change from first to last
  observation inside an epoch — degradation between recalibrations. (On real
  IBM data values are constant within an epoch, so this is 0; the mock's
  drifting backend exercises it.)

The metric drift score is the worst (max) of the three normalized signals.
All results land in the generic ``analysis_runs`` table as ``run_type="drift"``.
"""

from __future__ import annotations

import json
from datetime import datetime

import numpy as np
from pydantic import BaseModel, ConfigDict
from sqlalchemy.orm import Session

from app.analysis.series import (
    QUBIT_DRIFT_METRICS,
    MetricSeries,
    edge_error_series,
    filter_window,
    qubit_metric_series,
    undirected_edges,
)
from app.calibration.loaders import load_snapshots
from app.calibration.models import CalibrationSnapshot
from app.storage.tables import AnalysisRun
from app.utils.timeutils import utcnow

#: Normalization scales: a signal at this magnitude maps to score 1.0.
#: VOLATILITY_SCALE is set so ordinary recalibration variation (~15-25%
#: log-change between epochs, normal on real devices) scores ~0.3-0.4 rather
#: than saturating; only well beyond that does a series get flagged.
VOLATILITY_SCALE = 0.60  # std of log-changes across epochs
TREND_SCALE = 0.05  # |fractional change per day|
GROWTH_SCALE = 0.15  # |median within-epoch fractional growth|

#: Metric drift score above which a series is flagged.
FLAG_THRESHOLD = 0.5


class MetricDrift(BaseModel):
    """Drift statistics for one metric series."""

    model_config = ConfigDict(extra="forbid")

    metric: str
    n_points: int
    n_epochs: int
    n_recalibrations: int
    median_value: float | None = None
    volatility: float | None = None
    trend_per_day: float | None = None
    within_epoch_growth: float | None = None
    drift_score: float = 0.0
    flagged: bool = False


class QubitDrift(BaseModel):
    """Drift summary for one qubit across its metrics."""

    model_config = ConfigDict(extra="forbid")

    qubit_index: int
    metrics: list[MetricDrift]
    drift_score: float


class EdgeDrift(BaseModel):
    """Drift summary for one undirected coupling edge (2q gate error)."""

    model_config = ConfigDict(extra="forbid")

    q0: int
    q1: int
    drift: MetricDrift


class BackendDriftReport(BaseModel):
    """Full drift report for one backend over a time window."""

    model_config = ConfigDict(extra="forbid")

    backend_name: str
    window_days: float
    generated_at: datetime
    n_snapshots: int
    qubits: list[QubitDrift]
    edges: list[EdgeDrift]
    backend_drift_score: float


def _clip01(value: float) -> float:
    """Clamp to [0, 1]."""
    return max(0.0, min(1.0, value))


def compute_metric_drift(series: MetricSeries) -> MetricDrift:
    """Compute drift statistics for one metric series.

    Series with fewer than two points carry no drift information and score 0.
    """
    values = np.array([p.value for p in series.points], dtype=float)
    epochs = series.epochs()
    epoch_medians = [m for _, m in series.epoch_medians()]
    n_recal = max(0, len(epochs) - 1)

    result = MetricDrift(
        metric=series.metric,
        n_points=len(values),
        n_epochs=len(epochs),
        n_recalibrations=n_recal,
        median_value=float(np.median(values)) if len(values) else None,
    )
    if len(values) < 2 or np.any(values <= 0):
        return result

    scores: list[float] = []

    if len(epoch_medians) >= 2 and all(m > 0 for m in epoch_medians):
        log_changes = np.diff(np.log(epoch_medians))
        result.volatility = float(np.std(log_changes))
        scores.append(_clip01(result.volatility / VOLATILITY_SCALE))

    if len(values) >= 3:
        t0 = series.points[0].snapshot_ts
        days = np.array(
            [(p.snapshot_ts - t0).total_seconds() / 86400.0 for p in series.points]
        )
        if np.ptp(days) > 0:
            slope = float(np.polyfit(days, np.log(values), 1)[0])
            result.trend_per_day = slope
            scores.append(_clip01(abs(slope) / TREND_SCALE))

    growths = [
        (vals[-1] - vals[0]) / vals[0]
        for _, vals in epochs
        if len(vals) >= 2 and vals[0] > 0
    ]
    if growths:
        result.within_epoch_growth = float(np.median(growths))
        scores.append(_clip01(abs(result.within_epoch_growth) / GROWTH_SCALE))

    result.drift_score = max(scores) if scores else 0.0
    result.flagged = result.drift_score > FLAG_THRESHOLD
    return result


def compute_backend_drift(
    snapshots: list[CalibrationSnapshot], window_days: float = 14.0
) -> BackendDriftReport:
    """Compute the full drift report for one backend's snapshot history."""
    windowed = filter_window(snapshots, window_days)
    backend_name = windowed[0].backend_name if windowed else ""
    num_qubits = windowed[0].num_qubits if windowed else 0

    qubit_reports: list[QubitDrift] = []
    for index in range(num_qubits):
        metric_reports = [
            compute_metric_drift(qubit_metric_series(windowed, index, metric))
            for metric in QUBIT_DRIFT_METRICS
        ]
        informative = [m.drift_score for m in metric_reports if m.n_points >= 2]
        qubit_reports.append(
            QubitDrift(
                qubit_index=index,
                metrics=metric_reports,
                drift_score=float(np.mean(informative)) if informative else 0.0,
            )
        )

    edge_reports = [
        EdgeDrift(q0=q0, q1=q1, drift=compute_metric_drift(edge_error_series(windowed, q0, q1)))
        for q0, q1 in undirected_edges(windowed)
    ]

    qubit_scores = [q.drift_score for q in qubit_reports]
    return BackendDriftReport(
        backend_name=backend_name,
        window_days=window_days,
        generated_at=utcnow(),
        n_snapshots=len(windowed),
        qubits=qubit_reports,
        edges=edge_reports,
        backend_drift_score=float(np.mean(qubit_scores)) if qubit_scores else 0.0,
    )


def run_drift_analysis(
    session: Session, backend_name: str, window_days: float = 14.0
) -> tuple[int, BackendDriftReport]:
    """Compute drift for a stored backend and persist it as an analysis run.

    Returns ``(analysis_run_id, report)``.
    """
    snapshots = load_snapshots(session, backend_name=backend_name)
    if not snapshots:
        raise ValueError(f"no snapshots stored for backend {backend_name!r}")
    report = compute_backend_drift(snapshots, window_days=window_days)
    run = AnalysisRun(
        run_type="drift",
        params_json=json.dumps({"backend": backend_name, "window_days": window_days}),
        results_json=report.model_dump_json(),
    )
    session.add(run)
    session.flush()
    return run.id, report

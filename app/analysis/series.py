"""Build per-metric time series from stored calibration snapshots.

The central idea: each metric value carries its own ``calibrated_at``
timestamp, so a *calibration epoch* is a run of snapshots sharing the same
``calibrated_at`` for that metric. A change in ``calibrated_at`` is a
recalibration event — detected from the calibration date, never inferred
from value jumps.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass
from datetime import datetime, timedelta

from app.calibration.models import CalibrationSnapshot

#: Qubit metrics analyzed for drift (positive-valued, log-scale friendly).
QUBIT_DRIFT_METRICS: tuple[str, ...] = ("t1_us", "t2_us", "readout_error", "sx_gate_error")

#: Metric field -> its per-metric calibration timestamp field.
CALIBRATED_AT_FIELD: dict[str, str] = {
    "t1_us": "t1_calibrated_at",
    "t2_us": "t2_calibrated_at",
    "readout_error": "readout_error_calibrated_at",
    "sx_gate_error": "sx_gate_error_calibrated_at",
    "two_qubit_gate_error": "two_qubit_gate_error_calibrated_at",
}


@dataclass(frozen=True)
class MetricPoint:
    """One observation of a metric: snapshot time, value, calibration time."""

    snapshot_ts: datetime
    value: float
    calibrated_at: datetime | None


@dataclass(frozen=True)
class MetricSeries:
    """Time-ordered observations of one metric for one qubit or edge."""

    backend_name: str
    metric: str
    points: tuple[MetricPoint, ...]

    def epochs(self) -> list[tuple[datetime, list[float]]]:
        """Group points into calibration epochs by ``calibrated_at``.

        Returns ``(calibrated_at, values)`` per epoch in chronological order.
        Points without a ``calibrated_at`` are excluded (they cannot be
        assigned to an epoch).
        """
        grouped: dict[datetime, list[float]] = {}
        for point in self.points:
            if point.calibrated_at is not None:
                grouped.setdefault(point.calibrated_at, []).append(point.value)
        return sorted(grouped.items())

    def epoch_medians(self) -> list[tuple[datetime, float]]:
        """Median value per calibration epoch, in chronological order."""
        return [(ts, statistics.median(values)) for ts, values in self.epochs()]


def filter_window(
    snapshots: list[CalibrationSnapshot], window_days: float
) -> list[CalibrationSnapshot]:
    """Keep snapshots within ``window_days`` of the most recent snapshot."""
    if not snapshots:
        return []
    cutoff = max(s.timestamp for s in snapshots) - timedelta(days=window_days)
    return [s for s in snapshots if s.timestamp >= cutoff]


def qubit_metric_series(
    snapshots: list[CalibrationSnapshot], qubit_index: int, metric: str
) -> MetricSeries:
    """Series of one qubit metric across snapshots (missing values skipped)."""
    ts_field = CALIBRATED_AT_FIELD[metric]
    points: list[MetricPoint] = []
    for snap in snapshots:
        qubit = next((q for q in snap.qubits if q.qubit_index == qubit_index), None)
        if qubit is None:
            continue
        value = getattr(qubit, metric)
        if value is not None:
            points.append(MetricPoint(snap.timestamp, value, getattr(qubit, ts_field)))
    backend = snapshots[0].backend_name if snapshots else ""
    return MetricSeries(backend_name=backend, metric=metric, points=tuple(points))


def edge_error_series(
    snapshots: list[CalibrationSnapshot], q0: int, q1: int
) -> MetricSeries:
    """Series of the two-qubit gate error for one undirected edge.

    Coupling maps list both directions with identical calibration; the first
    matching direction per snapshot is used.
    """
    key = (min(q0, q1), max(q0, q1))
    points: list[MetricPoint] = []
    for snap in snapshots:
        edge = next(
            (e for e in snap.edges if (min(e.q0, e.q1), max(e.q0, e.q1)) == key), None
        )
        if edge is None or edge.two_qubit_gate_error is None:
            continue
        points.append(
            MetricPoint(
                snap.timestamp,
                edge.two_qubit_gate_error,
                edge.two_qubit_gate_error_calibrated_at,
            )
        )
    backend = snapshots[0].backend_name if snapshots else ""
    return MetricSeries(backend_name=backend, metric="two_qubit_gate_error", points=tuple(points))


def undirected_edges(snapshots: list[CalibrationSnapshot]) -> list[tuple[int, int]]:
    """All undirected coupling edges seen across the snapshots."""
    edges: set[tuple[int, int]] = set()
    for snap in snapshots:
        for q0, q1 in snap.coupling_map:
            edges.add((min(q0, q1), max(q0, q1)))
    return sorted(edges)

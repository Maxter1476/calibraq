"""Drift detection: recalibration awareness, trend detection, scoring."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from app.analysis.drift import BackendDriftReport, compute_metric_drift, run_drift_analysis
from app.analysis.series import MetricPoint, MetricSeries
from app.calibration.loaders import store_snapshot
from app.collector.mock import MOCK_BACKEND_SPECS, generate_history

T0 = datetime(2026, 6, 1, tzinfo=timezone.utc)
_SPEC_BY_NAME = {spec.name: spec for spec in MOCK_BACKEND_SPECS}


def _series(values_with_cal: list[tuple[float, int]]) -> MetricSeries:
    """Build a series: (value, epoch_id) pairs spaced 12h apart.

    ``epoch_id`` selects the calibrated_at timestamp, so recalibration events
    are encoded exactly the way real data encodes them.
    """
    points = [
        MetricPoint(
            snapshot_ts=T0 + timedelta(hours=12 * i),
            value=value,
            calibrated_at=T0 + timedelta(days=epoch_id),
        )
        for i, (value, epoch_id) in enumerate(values_with_cal)
    ]
    return MetricSeries(backend_name="synthetic", metric="t1_us", points=tuple(points))


def test_constant_series_scores_zero() -> None:
    """A flat single-epoch series carries no drift signal."""
    drift = compute_metric_drift(_series([(100.0, 0)] * 6))
    assert drift.n_epochs == 1
    assert drift.n_recalibrations == 0
    assert drift.drift_score < 1e-9  # regression on constant data gives float noise
    assert not drift.flagged


def test_recalibrations_counted_from_calibrated_at_not_value_jumps() -> None:
    """Epochs come from calibrated_at changes even when values barely move,
    and value wiggles within one epoch are not recalibrations."""
    # Three epochs, nearly identical values: 2 recalibrations detected.
    steady = compute_metric_drift(
        _series([(100.0, 0), (100.1, 0), (99.9, 1), (100.2, 1), (100.0, 2)])
    )
    assert steady.n_epochs == 3
    assert steady.n_recalibrations == 2

    # One epoch, big value wiggles: still zero recalibrations.
    wiggly = compute_metric_drift(_series([(100.0, 0), (60.0, 0), (140.0, 0), (80.0, 0)]))
    assert wiggly.n_recalibrations == 0


def test_recalibration_jumps_raise_volatility_score() -> None:
    """Large value resets across epochs produce a high, flagged drift score."""
    jumpy = compute_metric_drift(
        _series([(100.0, 0), (100.0, 0), (160.0, 1), (160.0, 1), (90.0, 2), (90.0, 2)])
    )
    assert jumpy.volatility is not None and jumpy.volatility > 0.3
    assert jumpy.flagged


def test_secular_trend_detected() -> None:
    """A steady decline shows a negative trend_per_day of the right size."""
    # 10% decay per 12h step within one epoch -> about -0.21/day on log scale.
    values = [(100.0 * (0.9**i), 0) for i in range(8)]
    drift = compute_metric_drift(_series(values))
    assert drift.trend_per_day is not None
    assert -0.25 < drift.trend_per_day < -0.15
    assert drift.flagged


def test_within_epoch_growth_detected() -> None:
    """Degradation between recalibrations is captured even when each
    recalibration resets the value (sawtooth, near-zero overall trend)."""
    sawtooth = []
    for epoch in range(4):
        sawtooth += [(0.010, epoch), (0.013, epoch), (0.016, epoch)]
    drift = compute_metric_drift(_series(sawtooth))
    assert drift.within_epoch_growth is not None
    assert drift.within_epoch_growth > 0.4  # +60% inside each epoch


def test_mock_profiles_rank_correctly(session: Session) -> None:
    """End-to-end on stored mock data: stable < drifting < unstable drift,
    and the report persists into analysis_runs and parses back."""
    for name in ("mock_stable_5q", "mock_drifting_5q", "mock_unstable_16q"):
        for snap in generate_history(_SPEC_BY_NAME[name], days=10, polls_per_day=2, seed=21):
            store_snapshot(session, snap)
    session.commit()

    _, stable = run_drift_analysis(session, "mock_stable_5q", window_days=10)
    _, drifting = run_drift_analysis(session, "mock_drifting_5q", window_days=10)
    run_id, unstable = run_drift_analysis(session, "mock_unstable_16q", window_days=10)
    session.commit()

    assert stable.backend_drift_score < drifting.backend_drift_score
    assert drifting.backend_drift_score < unstable.backend_drift_score

    from sqlalchemy import select

    from app.storage.tables import AnalysisRun

    run = session.get(AnalysisRun, run_id)
    assert run is not None and run.run_type == "drift"
    parsed = BackendDriftReport.model_validate_json(run.results_json)
    assert parsed.backend_name == "mock_unstable_16q"
    assert parsed.backend_drift_score == unstable.backend_drift_score

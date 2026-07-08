"""Health scoring: component behavior, ranking, persistence."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.analysis.health import BackendHealth, compute_backend_health, run_health_analysis
from app.calibration.loaders import store_snapshot
from app.calibration.models import CalibrationSnapshot, EdgeMetrics, QubitMetrics
from app.collector.mock import MOCK_BACKEND_SPECS, generate_history
from app.storage.tables import AnalysisRun

_SPEC_BY_NAME = {spec.name: spec for spec in MOCK_BACKEND_SPECS}
T0 = datetime(2026, 6, 1, tzinfo=timezone.utc)


def _tiny_snapshot(ts: datetime, q1_operational: bool) -> CalibrationSnapshot:
    """A 2-qubit snapshot where qubit 0 is good and qubit 1 is poor."""
    return CalibrationSnapshot(
        backend_name="tiny",
        timestamp=ts,
        num_qubits=2,
        coupling_map=[(0, 1)],
        qubits=[
            QubitMetrics(
                qubit_index=0, t1_us=180.0, t2_us=120.0,
                readout_error=0.005, sx_gate_error=1e-4, operational=True,
            ),
            QubitMetrics(
                qubit_index=1, t1_us=40.0, t2_us=20.0,
                readout_error=0.15, sx_gate_error=5e-3, operational=q1_operational,
            ),
        ],
        edges=[EdgeMetrics(q0=0, q1=1, gate_name="cz", two_qubit_gate_error=8e-3)],
        snapshot_source="mock",
    )


def test_better_qubit_scores_higher_and_outages_cut_uptime() -> None:
    """Component sanity on a hand-built backend."""
    snapshots = [
        _tiny_snapshot(T0 + timedelta(hours=12 * i), q1_operational=(i % 2 == 0))
        for i in range(6)
    ]
    health = compute_backend_health(snapshots, window_days=10)

    q0, q1 = health.qubits
    assert q0.health_score > q1.health_score
    assert q0.uptime == 1.0
    assert q1.uptime == 0.5
    assert health.median_two_qubit_error == 8e-3
    assert 0.0 <= health.health_score <= 100.0


def test_missing_components_renormalize_not_penalize() -> None:
    """A qubit with no sx/readout data is scored on what we know."""
    snap = CalibrationSnapshot(
        backend_name="sparse",
        timestamp=T0,
        num_qubits=1,
        coupling_map=[],
        qubits=[QubitMetrics(qubit_index=0, t1_us=180.0, t2_us=120.0, operational=True)],
        edges=[],
        snapshot_source="mock",
    )
    health = compute_backend_health([snap, snap.model_copy()], window_days=10)
    q0 = health.qubits[0]
    assert "readout" not in q0.components and "sx" not in q0.components
    # Good coherence + full uptime: the score should still be high.
    assert q0.health_score > 80.0


def test_stable_mock_outscores_unstable_and_persists(session: Session) -> None:
    """End-to-end ranking on stored mock data, persisted to analysis_runs."""
    for name in ("mock_stable_5q", "mock_unstable_16q"):
        for snap in generate_history(_SPEC_BY_NAME[name], days=10, polls_per_day=2, seed=33):
            store_snapshot(session, snap)
    session.commit()

    _, stable = run_health_analysis(session, "mock_stable_5q", window_days=10)
    run_id, unstable = run_health_analysis(session, "mock_unstable_16q", window_days=10)
    session.commit()

    assert stable.health_score > unstable.health_score
    assert unstable.operational_fraction < 1.0  # outage windows visible

    run = session.get(AnalysisRun, run_id)
    assert run is not None and run.run_type == "health"
    parsed = BackendHealth.model_validate_json(run.results_json)
    assert parsed.health_score == unstable.health_score

    # Exactly the two runs we created.
    assert len(list(session.scalars(select(AnalysisRun)))) == 2

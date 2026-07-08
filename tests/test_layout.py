"""Layout selection: shim fidelity, mapomatic baseline equivalence, drift rerank."""

from __future__ import annotations

from datetime import datetime, timezone

import mapomatic
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.analysis.drift import BackendDriftReport, EdgeDrift, MetricDrift, QubitDrift, compute_backend_drift
from app.calibration.loaders import store_snapshot
from app.calibration.models import CalibrationSnapshot, EdgeMetrics, QubitMetrics
from app.collector.mock import MOCK_BACKEND_SPECS, generate_history
from app.layout.backend_shim import SnapshotBackend
from app.layout.circuits import build_circuit
from app.layout.runner import run_layout_selection
from app.layout.selection import (
    candidate_layouts,
    make_drift_aware_cost,
    score_drift_aware,
    score_static,
)
from app.layout.validation import LayoutComparison, compare_layouts
from app.storage.tables import AnalysisRun, LayoutRecommendation

_SPEC_BY_NAME = {spec.name: spec for spec in MOCK_BACKEND_SPECS}
T0 = datetime(2026, 6, 1, tzinfo=timezone.utc)


def _line_snapshot(errors: dict[int, float], edge_errors: dict[tuple[int, int], float]) -> CalibrationSnapshot:
    """A 4-qubit line backend with explicit sx/readout and edge errors."""
    n = 4
    coupling = []
    for i in range(n - 1):
        coupling += [(i, i + 1), (i + 1, i)]
    qubits = [
        QubitMetrics(
            qubit_index=i,
            t1_us=120.0,
            t2_us=90.0,
            readout_error=errors.get(i, 0.01),
            sx_gate_error=errors.get(i, 0.01) / 10,
            operational=True,
        )
        for i in range(n)
    ]
    edges = []
    for i in range(n - 1):
        err = edge_errors.get((i, i + 1), 0.01)
        for a, b in ((i, i + 1), (i + 1, i)):
            edges.append(EdgeMetrics(q0=a, q1=b, gate_name="cz", two_qubit_gate_error=err))
    return CalibrationSnapshot(
        backend_name="line4",
        timestamp=T0,
        num_qubits=n,
        coupling_map=coupling,
        qubits=qubits,
        edges=edges,
        snapshot_source="mock",
    )


def _zero_drift_report(snapshot: CalibrationSnapshot) -> BackendDriftReport:
    """A drift report with every score zero (drift-aware == baseline)."""
    qubits = [
        QubitDrift(
            qubit_index=q.qubit_index,
            metrics=[
                MetricDrift(metric=m, n_points=2, n_epochs=1, n_recalibrations=0, drift_score=0.0)
                for m in ("t1_us", "t2_us", "readout_error", "sx_gate_error")
            ],
            drift_score=0.0,
        )
        for q in snapshot.qubits
    ]
    edges = [
        EdgeDrift(
            q0=min(e.q0, e.q1),
            q1=max(e.q0, e.q1),
            drift=MetricDrift(
                metric="two_qubit_gate_error", n_points=2, n_epochs=1, n_recalibrations=0, drift_score=0.0
            ),
        )
        for e in snapshot.edges
        if e.q0 < e.q1
    ]
    return BackendDriftReport(
        backend_name=snapshot.backend_name,
        window_days=14.0,
        generated_at=T0,
        n_snapshots=1,
        qubits=qubits,
        edges=edges,
        backend_drift_score=0.0,
    )


def test_shim_reconstruction_feeds_mapomatic_exact_cost() -> None:
    """A 2q circuit's mapomatic cost equals the hand-computed infidelity."""
    snap = _line_snapshot(errors={0: 0.02, 1: 0.03}, edge_errors={(0, 1): 0.05})
    backend = SnapshotBackend(snap)
    qc = build_circuit("linear", 2, gate_name="cz")  # sx,sx, cz(0,1), measure_all

    scored = score_static(qc, backend)
    layout01 = next(cost for layout, cost in scored if layout == [0, 1])

    # Expected fidelity for layout [0,1]: two sx (err 0.002, 0.003), one cz
    # (0.05), two readout (0.02, 0.03).
    fid = (1 - 0.002) * (1 - 0.003) * (1 - 0.05) * (1 - 0.02) * (1 - 0.03)
    assert layout01 == pytest.approx(1 - fid, rel=1e-9)


def test_drift_aware_with_zero_risk_equals_mapomatic_baseline() -> None:
    """k=0 reproduces mapomatic default_cost exactly for every layout."""
    snap = _line_snapshot(errors={}, edge_errors={(0, 1): 0.02, (1, 2): 0.04, (2, 3): 0.01})
    backend = SnapshotBackend(snap)
    qc = build_circuit("linear", 3, gate_name="cz")
    layouts = candidate_layouts(qc, backend)

    baseline = dict((tuple(layout), cost) for layout, cost in score_static(qc, backend, layouts=layouts))
    drift_k0 = dict(
        (tuple(layout), cost)
        for layout, cost in score_drift_aware(
            qc, backend, _zero_drift_report(snap), risk_factor=0.0, layouts=layouts
        )
    )
    assert baseline.keys() == drift_k0.keys()
    for layout, cost in baseline.items():
        assert drift_k0[layout] == pytest.approx(cost, rel=1e-12)


def test_drift_penalty_reranks_toward_low_drift_resources() -> None:
    """Two equal-error edges: drift on one pushes selection to the other."""
    # Edges (0,1) and (2,3) have identical low error; (1,2) is bad so the
    # circuit (linear-2) will prefer one of the good edges.
    snap = _line_snapshot(errors={}, edge_errors={(0, 1): 0.02, (1, 2): 0.5, (2, 3): 0.02})
    backend = SnapshotBackend(snap)
    qc = build_circuit("linear", 2, gate_name="cz")
    layouts = candidate_layouts(qc, backend)

    # Build a drift report where edge (0,1) is highly drifting, (2,3) stable.
    report = _zero_drift_report(snap)
    for edge in report.edges:
        if (edge.q0, edge.q1) == (0, 1):
            edge.drift.drift_score = 0.9

    static = score_static(qc, backend, layouts=layouts)
    drift = score_drift_aware(qc, backend, report, risk_factor=2.0, layouts=layouts)

    # Static is ~tied between [0,1]/[1,0] and [2,3]/[3,2]; drift-aware must put
    # a layout using edge (2,3) on top, never one using the drifting (0,1).
    drift_best = set(drift[0][0])
    assert drift_best in ({2, 3},)


def test_compare_layouts_summary_and_persistence(session: Session) -> None:
    """End-to-end on stored mock data: comparison persists and parses back."""
    spec = _SPEC_BY_NAME["mock_unstable_16q"]
    for snap in generate_history(spec, days=8, polls_per_day=2, seed=5):
        store_snapshot(session, snap)
    session.commit()

    comp, ids = run_layout_selection(
        session, "mock_unstable_16q", kind="linear", n_qubits=4, risk_factor=1.0, window_days=8
    )
    session.commit()

    assert comp.n_candidates > 0
    assert -1.0 <= comp.kendall_tau <= 1.0
    assert comp.static_cost_delta >= -1e-9  # drift choice can't beat static on static cost
    assert 0.0 <= comp.top3_overlap <= 1.0

    run = session.get(AnalysisRun, ids["validation"])
    assert run is not None and run.run_type == "layout_validation"
    parsed = LayoutComparison.model_validate_json(run.results_json)
    assert parsed.backend_name == "mock_unstable_16q"
    assert parsed.drift_best_layout == comp.drift_best_layout

    recs = list(session.scalars(select(LayoutRecommendation)))
    methods = {r.method for r in recs}
    assert "mapomatic_static" in methods
    assert any(m.startswith("drift_aware") for m in methods)


def test_drift_choice_never_worse_on_drift_cost(session: Session) -> None:
    """The drift-aware top choice minimizes drift cost by construction."""
    spec = _SPEC_BY_NAME["mock_drifting_5q"]
    snapshots = generate_history(spec, days=8, polls_per_day=2, seed=9)
    latest = snapshots[-1]
    drift_report = compute_backend_drift(snapshots, window_days=8)
    qc = build_circuit("linear", 3, gate_name=latest.edges[0].gate_name)
    comp = compare_layouts(qc, "linear-3", latest, drift_report, risk_factor=1.5)
    # Switching to the drift choice reduces (or ties) drift exposure.
    assert comp.drift_exposure_reduction >= -1e-9

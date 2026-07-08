"""Compare drift-aware layout selection against the mapomatic baseline.

This harness runs both rankings on the same calibration snapshot and quantifies
how the drift-aware cost reranks relative to mapomatic's drift-blind default:
how often the top choice changes, the rank correlation across all candidates,
and the trade-off the drift-aware choice makes (it accepts slightly higher
latest-snapshot infidelity in exchange for lower exposure to drifting
resources).

IMPORTANT — what this does and does not establish. It is a *descriptive*
comparison of two scoring functions on stored data. It does NOT establish that
drift-aware selection yields better real-world results: that claim requires
running the chosen layouts on real hardware at job time and measuring fidelity,
and (per the project rule) it may only be made from real IBM data, never mock.
On mock data this harness validates the machinery and characterizes behavior;
it makes no predictive claim. See PROJECT_STATE.md.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict
from qiskit import QuantumCircuit

from app.analysis.drift import BackendDriftReport
from app.calibration.models import CalibrationSnapshot
from app.layout.backend_shim import SnapshotBackend
from app.layout.selection import (
    DEFAULT_RISK_FACTOR,
    ScoredLayout,
    candidate_layouts,
    score_drift_aware,
    score_static,
)
from app.utils.timeutils import utcnow


class LayoutComparison(BaseModel):
    """Descriptive comparison of drift-aware vs. mapomatic-baseline ranking."""

    model_config = ConfigDict(extra="forbid")

    backend_name: str
    circuit_descriptor: str
    snapshot_timestamp: datetime
    snapshot_source: str
    generated_at: datetime
    risk_factor: float
    n_candidates: int

    static_best_layout: list[int]
    static_best_cost: float
    drift_best_layout: list[int]
    drift_best_cost: float

    same_choice: bool
    drift_best_rank_in_static: int  # 0 = also the static best
    # Trade-off of switching to the drift-aware choice, on this snapshot:
    static_cost_delta: float  # drift choice's static cost - static best's static cost (>= 0)
    drift_exposure_reduction: float  # static best's drift cost - drift choice's drift cost
    kendall_tau: float  # rank correlation of the two orderings over all candidates
    top3_overlap: float  # fraction of top-3 layouts shared between the rankings


def _kendall_tau(order_a: list[tuple[int, ...]], order_b: list[tuple[int, ...]]) -> float:
    """Kendall tau-b rank correlation between two orderings of the same items.

    Items are keyed by their layout tuple. Returns 1.0 for identical orderings,
    -1.0 for fully reversed; 1.0 for trivial (<2 item) inputs.
    """
    rank_b = {item: i for i, item in enumerate(order_b)}
    items = [item for item in order_a if item in rank_b]
    n = len(items)
    if n < 2:
        return 1.0
    a_ranks = list(range(len(items)))
    b_ranks = [rank_b[item] for item in items]
    concordant = discordant = 0
    for i in range(n):
        for j in range(i + 1, n):
            sign_a = a_ranks[i] - a_ranks[j]
            sign_b = b_ranks[i] - b_ranks[j]
            product = sign_a * sign_b
            if product > 0:
                concordant += 1
            elif product < 0:
                discordant += 1
    total = concordant + discordant
    return (concordant - discordant) / total if total else 1.0


def _rank_of(layout: list[int], ranking: list[ScoredLayout]) -> int:
    """Index of ``layout`` within a scored ranking (by exact layout match)."""
    target = list(layout)
    for i, (candidate, _cost) in enumerate(ranking):
        if candidate == target:
            return i
    return len(ranking)


def compare_layouts(
    circuit: QuantumCircuit,
    circuit_descriptor: str,
    snapshot: CalibrationSnapshot,
    drift_report: BackendDriftReport,
    risk_factor: float = DEFAULT_RISK_FACTOR,
) -> LayoutComparison:
    """Run both rankings on one snapshot and summarize how they differ.

    ``snapshot`` should be the latest snapshot (the calibration a job would see
    at submit time); ``drift_report`` is the phase-2 drift report for the same
    backend over the analysis window.
    """
    backend = SnapshotBackend(snapshot)
    layouts = candidate_layouts(circuit, backend)
    if not layouts:
        raise ValueError(
            f"no valid layouts for circuit {circuit_descriptor!r} on {snapshot.backend_name!r}"
        )

    static_ranking = score_static(circuit, backend, layouts=layouts)
    drift_ranking = score_drift_aware(
        circuit, backend, drift_report, risk_factor=risk_factor, layouts=layouts
    )
    static_cost = {tuple(layout): cost for layout, cost in static_ranking}
    drift_cost = {tuple(layout): cost for layout, cost in drift_ranking}

    static_best_layout, static_best_cost = static_ranking[0]
    drift_best_layout, drift_best_cost = drift_ranking[0]

    static_order = [tuple(layout) for layout, _ in static_ranking]
    drift_order = [tuple(layout) for layout, _ in drift_ranking]
    top3_static = set(static_order[:3])
    top3_drift = set(drift_order[:3])
    overlap = len(top3_static & top3_drift) / max(1, min(3, len(layouts)))

    return LayoutComparison(
        backend_name=snapshot.backend_name,
        circuit_descriptor=circuit_descriptor,
        snapshot_timestamp=snapshot.timestamp,
        snapshot_source=snapshot.snapshot_source,
        generated_at=utcnow(),
        risk_factor=risk_factor,
        n_candidates=len(layouts),
        static_best_layout=static_best_layout,
        static_best_cost=static_best_cost,
        drift_best_layout=drift_best_layout,
        drift_best_cost=drift_best_cost,
        same_choice=(static_best_layout == drift_best_layout),
        drift_best_rank_in_static=_rank_of(drift_best_layout, static_ranking),
        static_cost_delta=static_cost[tuple(drift_best_layout)] - static_best_cost,
        drift_exposure_reduction=drift_cost[tuple(static_best_layout)] - drift_best_cost,
        kendall_tau=_kendall_tau(static_order, drift_order),
        top3_overlap=overlap,
    )

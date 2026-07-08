"""Drift-aware layout selection, built on top of mapomatic.

mapomatic is the trusted baseline: its vf2 ``matching_layouts`` enumerates
every valid placement of a circuit on a backend's coupling map, and its
``default_cost`` scores a layout by expected circuit infidelity on a single
calibration snapshot. CalibraQ's contribution is a *drift-aware* cost function
that plugs into mapomatic's own ``cost_function`` extension point.

The drift-aware cost reuses mapomatic's infidelity model but replaces each
resource's latest error ``e`` with a drift-inflated effective error
``e * (1 + k * drift_score)``, where ``drift_score in [0, 1]`` comes from the
phase-2 recalibration-aware drift report and ``k`` is a risk-aversion factor.
Rationale: a layout that looks good on the latest snapshot but sits on
high-drift qubits/edges is risky, because by the time a job actually runs
(hours after calibration) those metrics may have degraded. With ``k = 0`` the
cost reduces *exactly* to mapomatic's ``default_cost`` — so drift-blind
selection is mapomatic, and drift-aware selection is a strict, documented
extension of it.

``k`` is a heuristic knob, not a validated constant. Whether drift-aware
reranking actually improves real-world fidelity is a phase-3 validation
question that requires real hardware runs (see PROJECT_STATE.md); nothing here
is a validated claim.
"""

from __future__ import annotations

from typing import Callable

import mapomatic
from qiskit import QuantumCircuit

from app.analysis.drift import BackendDriftReport
from app.calibration.models import CalibrationSnapshot
from app.layout.backend_shim import SnapshotBackend

#: Default risk-aversion factor for the drift-aware cost (heuristic).
DEFAULT_RISK_FACTOR = 1.0

#: Type of a mapomatic-compatible cost function.
CostFunction = Callable[[QuantumCircuit, list, object], list]

#: A scored layout: physical qubits (virtual index -> physical), and cost.
ScoredLayout = tuple[list[int], float]


def candidate_layouts(
    circuit: QuantumCircuit, backend: SnapshotBackend, strict_direction: bool = False
) -> list[list[int]]:
    """Enumerate valid layouts for ``circuit`` on ``backend`` via mapomatic.

    Returns mapomatic's vf2 subgraph matches; each layout maps the circuit's
    virtual qubit index to a physical qubit on the backend.
    """
    return mapomatic.matching_layouts(
        circuit, backend.coupling_map, strict_direction=strict_direction
    )


def score_static(
    circuit: QuantumCircuit, backend: SnapshotBackend, layouts: list[list[int]] | None = None
) -> list[ScoredLayout]:
    """Score layouts by mapomatic's default cost on this snapshot (drift-blind).

    This is the mapomatic baseline, used unchanged. Results are sorted by
    ascending cost (best first).
    """
    if layouts is None:
        layouts = candidate_layouts(circuit, backend)
    return [(list(layout), cost) for layout, cost in mapomatic.evaluate_layouts(circuit, layouts, backend)]


def _drift_lookups(
    drift_report: BackendDriftReport,
) -> tuple[dict[int, dict[str, float]], dict[tuple[int, int], float]]:
    """Build (qubit metric -> drift score) and (edge -> drift score) lookups."""
    qubit_drift: dict[int, dict[str, float]] = {}
    for qubit in drift_report.qubits:
        qubit_drift[qubit.qubit_index] = {m.metric: m.drift_score for m in qubit.metrics}
    edge_drift: dict[tuple[int, int], float] = {
        (min(e.q0, e.q1), max(e.q0, e.q1)): e.drift.drift_score for e in drift_report.edges
    }
    return qubit_drift, edge_drift


def make_drift_aware_cost(
    drift_report: BackendDriftReport, risk_factor: float = DEFAULT_RISK_FACTOR
) -> CostFunction:
    """Build a mapomatic-compatible cost function that inflates errors by drift.

    The returned callable mirrors mapomatic's ``default_cost`` exactly, except
    each error term ``e`` is replaced by ``e * (1 + risk_factor * drift)`` for
    the drift score of the resource that instruction uses. With
    ``risk_factor == 0`` it is identical to ``default_cost``.
    """
    qubit_drift, edge_drift = _drift_lookups(drift_report)

    def _inflate(error: float, drift: float) -> float:
        """Drift-inflate an error term, clamped to a valid probability."""
        return min(1.0, error * (1.0 + risk_factor * drift))

    def cost_function(circ: QuantumCircuit, layouts: list, backend: object) -> list[ScoredLayout]:
        props = backend.properties()  # type: ignore[attr-defined]
        out: list[ScoredLayout] = []
        for layout in layouts:
            fid = 1.0
            for instruction in circ.data:
                op = instruction.operation
                qubits = instruction.qubits
                if op.num_qubits == 2 and op.name != "barrier":
                    q0 = circ.find_bit(qubits[0]).index
                    q1 = circ.find_bit(qubits[1]).index
                    p0, p1 = layout[q0], layout[q1]
                    drift = edge_drift.get((min(p0, p1), max(p0, p1)), 0.0)
                    error = props.gate_error(op.name, [p0, p1])
                    fid *= 1 - _inflate(error, drift)
                elif op.name in ("sx", "x"):
                    p0 = layout[circ.find_bit(qubits[0]).index]
                    drift = qubit_drift.get(p0, {}).get("sx_gate_error", 0.0)
                    fid *= 1 - _inflate(props.gate_error(op.name, p0), drift)
                elif op.name in ("measure", "reset"):
                    p0 = layout[circ.find_bit(qubits[0]).index]
                    drift = qubit_drift.get(p0, {}).get("readout_error", 0.0)
                    fid *= 1 - _inflate(props.readout_error(p0), drift)
            out.append((list(layout), 1 - fid))
        return out

    return cost_function


def score_drift_aware(
    circuit: QuantumCircuit,
    backend: SnapshotBackend,
    drift_report: BackendDriftReport,
    risk_factor: float = DEFAULT_RISK_FACTOR,
    layouts: list[list[int]] | None = None,
) -> list[ScoredLayout]:
    """Score layouts by the drift-aware cost on this snapshot.

    Results are sorted by ascending drift-aware cost (best first).
    """
    if layouts is None:
        layouts = candidate_layouts(circuit, backend)
    cost_function = make_drift_aware_cost(drift_report, risk_factor=risk_factor)
    return [
        (list(layout), cost)
        for layout, cost in mapomatic.evaluate_layouts(
            circuit, layouts, backend, cost_function=cost_function
        )
    ]

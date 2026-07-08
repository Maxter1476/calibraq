"""Built-in benchmark circuits for layout selection.

Circuits are constructed directly in an IBM-style basis (``sx`` + a named
two-qubit gate + ``measure``) so they need no transpilation before mapomatic
layout matching. The two-qubit gate is named to match the target backend
(``cz``/``ecr``/``cx``), because mapomatic's cost function looks up gate error
by the instruction's name.
"""

from __future__ import annotations

from qiskit import QuantumCircuit

#: Interaction topologies the builder supports.
CIRCUIT_KINDS = ("linear", "ring", "star")


def _apply_two_qubit(qc: QuantumCircuit, gate_name: str, q0: int, q1: int) -> None:
    """Append the named two-qubit gate to ``qc`` on ``(q0, q1)``."""
    if gate_name == "cz":
        qc.cz(q0, q1)
    elif gate_name == "ecr":
        qc.ecr(q0, q1)
    elif gate_name == "cx":
        qc.cx(q0, q1)
    else:
        raise ValueError(f"unsupported two-qubit gate {gate_name!r} (expected cz/ecr/cx)")


def build_circuit(kind: str, n_qubits: int, gate_name: str = "cz") -> QuantumCircuit:
    """Build a benchmark circuit of the given interaction topology.

    Args:
        kind: ``"linear"`` (path), ``"ring"`` (cycle), or ``"star"`` (hub).
        n_qubits: number of qubits (>= 2; >= 3 for ``ring``).
        gate_name: two-qubit gate name to emit (``cz``/``ecr``/``cx``).

    Returns:
        A circuit using ``sx``, the named two-qubit gate, and a final
        measurement, with an interaction graph matching ``kind``.
    """
    if n_qubits < 2:
        raise ValueError("n_qubits must be >= 2")
    if kind == "ring" and n_qubits < 3:
        raise ValueError("ring requires n_qubits >= 3")
    if kind not in CIRCUIT_KINDS:
        raise ValueError(f"unknown circuit kind {kind!r} (expected one of {CIRCUIT_KINDS})")

    qc = QuantumCircuit(n_qubits, name=f"{kind}-{n_qubits}")
    for q in range(n_qubits):
        qc.sx(q)

    if kind == "linear":
        for q in range(n_qubits - 1):
            _apply_two_qubit(qc, gate_name, q, q + 1)
    elif kind == "ring":
        for q in range(n_qubits):
            _apply_two_qubit(qc, gate_name, q, (q + 1) % n_qubits)
    elif kind == "star":
        for q in range(1, n_qubits):
            _apply_two_qubit(qc, gate_name, 0, q)

    qc.measure_all()
    return qc


def describe(kind: str, n_qubits: int) -> str:
    """Return a stable circuit descriptor string, e.g. ``"linear-3"``."""
    return f"{kind}-{n_qubits}"

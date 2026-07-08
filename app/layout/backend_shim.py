"""Bridge stored calibration snapshots into the mapomatic / qiskit interface.

mapomatic evaluates layouts against a backend object exposing
``configuration().basis_gates`` and ``properties()`` (a qiskit
``BackendProperties`` with ``gate_error``/``readout_error``). This module
reconstructs a real ``BackendProperties`` from one of our
:class:`CalibrationSnapshot` rows and wraps it in a minimal backend shim, so
mapomatic's proven vf2 layout matching and cost machinery run unchanged
against the data we collected.

The reconstruction is deliberately lossless for the fields mapomatic reads:
per-qubit ``readout_error`` and single-qubit ``sx``/``x`` gate errors, plus
two-qubit gate errors on every coupling edge (mirrored to both directed
orderings so direction-sensitive lookups always resolve).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from qiskit.transpiler import CouplingMap
from qiskit_ibm_runtime.models.backend_properties import (
    BackendProperties,
    GateProperties,
    Nduv,
)

from app.calibration.models import CalibrationSnapshot
from app.utils.timeutils import to_utc, utcnow

#: Single-qubit basis gates we attach the qubit's sx error to (x mirrors sx on
#: IBM hardware: both are derived from the same pi/2 pulse calibration).
SINGLE_QUBIT_GATES = ("sx", "x")

#: Fallback error used when a metric is missing, so layout scoring still runs
#: but strongly penalizes the unknown resource rather than treating it as ideal.
MISSING_ERROR = 0.5


def snapshot_to_backend_properties(snapshot: CalibrationSnapshot) -> BackendProperties:
    """Reconstruct a qiskit ``BackendProperties`` from a calibration snapshot.

    Only the fields mapomatic's cost functions read are populated. Missing
    metrics fall back to :data:`MISSING_ERROR` so scoring never silently treats
    an uncharacterized qubit or gate as error-free.
    """
    last_update = to_utc(snapshot.timestamp) or utcnow()

    qubit_props: list[list[Nduv]] = []
    for qubit in sorted(snapshot.qubits, key=lambda q: q.qubit_index):
        readout = qubit.readout_error if qubit.readout_error is not None else MISSING_ERROR
        date = to_utc(qubit.readout_error_calibrated_at) or last_update
        entries = [Nduv(date=date, name="readout_error", unit="", value=readout)]
        if qubit.t1_us is not None:
            entries.append(Nduv(date=last_update, name="T1", unit="us", value=qubit.t1_us))
        if qubit.t2_us is not None:
            entries.append(Nduv(date=last_update, name="T2", unit="us", value=qubit.t2_us))
        qubit_props.append(entries)

    gate_props: list[GateProperties] = []
    for qubit in sorted(snapshot.qubits, key=lambda q: q.qubit_index):
        sx_error = qubit.sx_gate_error if qubit.sx_gate_error is not None else MISSING_ERROR
        date = to_utc(qubit.sx_gate_error_calibrated_at) or last_update
        for gate_name in SINGLE_QUBIT_GATES:
            gate_props.append(
                GateProperties(
                    qubits=[qubit.qubit_index],
                    gate=gate_name,
                    parameters=[Nduv(date=date, name="gate_error", unit="", value=sx_error)],
                )
            )

    # Two-qubit gates: mirror each undirected edge to both directed orderings
    # with the same error, so default_cost's direction-sensitive lookup always
    # resolves regardless of which orientation a candidate layout uses.
    seen: set[tuple[int, int]] = set()
    for edge in snapshot.edges:
        if edge.two_qubit_gate_error is None:
            continue
        date = to_utc(edge.two_qubit_gate_error_calibrated_at) or last_update
        for pair in ((edge.q0, edge.q1), (edge.q1, edge.q0)):
            if pair in seen:
                continue
            seen.add(pair)
            gate_props.append(
                GateProperties(
                    qubits=list(pair),
                    gate=edge.gate_name,
                    parameters=[
                        Nduv(date=date, name="gate_error", unit="", value=edge.two_qubit_gate_error)
                    ],
                )
            )

    return BackendProperties(
        backend_name=snapshot.backend_name,
        backend_version="calibraq-reconstructed",
        last_update_date=last_update,
        qubits=qubit_props,
        gates=gate_props,
        general=[],
    )


class _ShimConfiguration:
    """Minimal stand-in for ``backend.configuration()`` used by mapomatic."""

    def __init__(self, n_qubits: int, basis_gates: list[str], coupling_map: list[list[int]]):
        self.n_qubits = n_qubits
        self.num_qubits = n_qubits
        self.basis_gates = basis_gates
        self.coupling_map = coupling_map


class SnapshotBackend:
    """A minimal backend over one calibration snapshot, for mapomatic.

    Exposes exactly the surface mapomatic touches: ``name``, ``num_qubits``,
    ``coupling_map`` (a qiskit ``CouplingMap``), ``configuration()`` (with
    ``basis_gates``), and ``properties()`` (the reconstructed
    ``BackendProperties``).
    """

    def __init__(self, snapshot: CalibrationSnapshot):
        self._snapshot = snapshot
        self.name = snapshot.backend_name
        self.num_qubits = snapshot.num_qubits
        self._properties = snapshot_to_backend_properties(snapshot)
        two_qubit_gates = sorted({e.gate_name for e in snapshot.edges})
        self._basis_gates = ["id", "rz", "sx", "x", "measure", "reset", *two_qubit_gates]
        self._coupling_list = [list(edge) for edge in snapshot.coupling_map]
        self.coupling_map = CouplingMap(couplinglist=self._coupling_list)

    @property
    def snapshot(self) -> CalibrationSnapshot:
        """The snapshot this backend was built from."""
        return self._snapshot

    def configuration(self) -> _ShimConfiguration:
        """Return a minimal configuration object (mapomatic reads basis_gates)."""
        return _ShimConfiguration(self.num_qubits, self._basis_gates, self._coupling_list)

    def properties(self, refresh: bool = False, datetime: datetime | None = None) -> BackendProperties:
        """Return the reconstructed backend properties (signature matches qiskit)."""
        return self._properties

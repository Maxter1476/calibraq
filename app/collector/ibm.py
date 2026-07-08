"""Normalize IBM Quantum backend calibration data into CalibrationSnapshot.

Verified against qiskit-ibm-runtime 0.47.0 (pinned in pyproject.toml):

- ``QiskitRuntimeService(channel="ibm_quantum_platform", token=...)`` — the
  legacy ``ibm_quantum`` channel no longer exists.
- ``IBMBackend.properties()`` returns
  ``qiskit_ibm_runtime.models.BackendProperties | None`` with:
  ``last_update_date``, ``qubits`` (list per qubit of ``Nduv`` entries, each
  with ``name``/``date``/``unit``/``value``), and ``gates`` (list of
  ``GateProperties`` with ``gate``/``qubits``/``parameters`` Nduvs).
- ``IBMBackend.coupling_map`` is a ``qiskit.transpiler.CouplingMap``.

Each ``Nduv`` carries its own calibration ``date`` — that per-metric timestamp
is preserved into the model because metrics are calibrated at different times.

This module deliberately avoids importing qiskit at module import time so the
rest of the app works without the dependency resolved (e.g. minimal CI).
"""

from __future__ import annotations

import logging
from typing import Any

from app.calibration.models import CalibrationSnapshot, EdgeMetrics, QubitMetrics

logger = logging.getLogger(__name__)

#: Nduv name -> (QubitMetrics value field, QubitMetrics timestamp field, target unit)
_QUBIT_NDUV_MAP: dict[str, tuple[str, str, str | None]] = {
    "T1": ("t1_us", "t1_calibrated_at", "us"),
    "T2": ("t2_us", "t2_calibrated_at", "us"),
    "frequency": ("frequency_ghz", "frequency_calibrated_at", "GHz"),
    "anharmonicity": ("anharmonicity_ghz", "anharmonicity_calibrated_at", "GHz"),
    "readout_error": ("readout_error", "readout_error_calibrated_at", None),
}

_TIME_TO_US = {"s": 1e6, "ms": 1e3, "us": 1.0, "µs": 1.0, "ns": 1e-3}
_TIME_TO_NS = {"s": 1e9, "ms": 1e6, "us": 1e3, "µs": 1e3, "ns": 1.0}
_FREQ_TO_GHZ = {"Hz": 1e-9, "kHz": 1e-6, "MHz": 1e-3, "GHz": 1.0}

#: Two-qubit gate names we recognize on IBM hardware.
TWO_QUBIT_GATES = ("cz", "ecr", "cx")


def _convert(value: float, unit: str, target: str | None) -> float | None:
    """Convert ``value`` with IBM-reported ``unit`` into the target unit.

    Returns ``None`` (and logs) for units we do not recognize, rather than
    storing a number in an unknown unit.
    """
    if target is None:
        return value
    table = {"us": _TIME_TO_US, "ns": _TIME_TO_NS, "GHz": _FREQ_TO_GHZ}[target]
    factor = table.get(unit)
    if factor is None:
        if unit == "":  # dimensionless reported with empty unit
            return value
        logger.warning("Unrecognized unit %r for target %s; dropping value", unit, target)
        return None
    return value * factor


def _qubit_metrics_from_nduvs(
    qubit_index: int,
    nduvs: list[Any],
    sx_errors: dict[int, tuple[float, Any]],
) -> QubitMetrics:
    """Build QubitMetrics for one qubit from its Nduv list plus sx gate errors."""
    fields: dict[str, Any] = {"qubit_index": qubit_index}
    for nduv in nduvs:
        mapping = _QUBIT_NDUV_MAP.get(nduv.name)
        if mapping is not None:
            value_field, ts_field, target_unit = mapping
            converted = _convert(nduv.value, nduv.unit, target_unit)
            if converted is not None:
                fields[value_field] = converted
                fields[ts_field] = nduv.date
        elif nduv.name == "operational":
            fields["operational"] = bool(nduv.value)
            fields["operational_calibrated_at"] = nduv.date
    if qubit_index in sx_errors:
        error, date = sx_errors[qubit_index]
        fields["sx_gate_error"] = error
        fields["sx_gate_error_calibrated_at"] = date
    return QubitMetrics(**fields)


def _gate_parameter(gate_props: Any, name: str) -> Any | None:
    """Return the Nduv named ``name`` from a GateProperties entry, if present."""
    for param in gate_props.parameters:
        if param.name == name:
            return param
    return None


def normalize_properties(
    backend_name: str,
    num_qubits: int,
    coupling_map: list[tuple[int, int]],
    properties: Any,
) -> CalibrationSnapshot:
    """Normalize a ``BackendProperties`` payload into a CalibrationSnapshot.

    ``properties`` is a ``qiskit_ibm_runtime.models.BackendProperties``; typed
    as ``Any`` so this module imports without qiskit installed.
    """
    # Single-qubit sx errors, keyed by qubit.
    sx_errors: dict[int, tuple[float, Any]] = {}
    # Two-qubit gate metrics keyed by directed qubit pair.
    edge_gates: dict[tuple[int, int], Any] = {}
    for gate in properties.gates:
        qubits = tuple(gate.qubits)
        if gate.gate == "sx" and len(qubits) == 1:
            err = _gate_parameter(gate, "gate_error")
            if err is not None:
                sx_errors[qubits[0]] = (err.value, err.date)
        elif gate.gate in TWO_QUBIT_GATES and len(qubits) == 2:
            edge_gates[(qubits[0], qubits[1])] = gate

    qubits = [
        _qubit_metrics_from_nduvs(index, nduvs, sx_errors)
        for index, nduvs in enumerate(properties.qubits)
    ]

    edges: list[EdgeMetrics] = []
    for q0, q1 in coupling_map:
        gate = edge_gates.get((q0, q1)) or edge_gates.get((q1, q0))
        if gate is None:
            continue  # edge with no calibrated 2q gate (e.g. faulty); no metrics row
        fields: dict[str, Any] = {"q0": q0, "q1": q1, "gate_name": gate.gate}
        err = _gate_parameter(gate, "gate_error")
        if err is not None:
            fields["two_qubit_gate_error"] = err.value
            fields["two_qubit_gate_error_calibrated_at"] = err.date
        length = _gate_parameter(gate, "gate_length")
        if length is not None:
            duration = _convert(length.value, length.unit, "ns")
            if duration is not None:
                fields["gate_duration_ns"] = duration
                fields["gate_duration_calibrated_at"] = length.date
        edges.append(EdgeMetrics(**fields))

    return CalibrationSnapshot(
        backend_name=backend_name,
        timestamp=properties.last_update_date,
        num_qubits=num_qubits,
        coupling_map=coupling_map,
        qubits=qubits,
        edges=edges,
        snapshot_source="ibm",
    )


def snapshot_from_backend(backend: Any) -> CalibrationSnapshot | None:
    """Build a CalibrationSnapshot from a live ``IBMBackend``.

    Returns ``None`` for simulators and backends that expose no calibration
    properties.
    """
    if getattr(backend, "simulator", False):
        return None
    properties = backend.properties()
    if properties is None:
        return None
    coupling = backend.coupling_map
    coupling_edges: list[tuple[int, int]] = (
        [tuple(edge) for edge in coupling.get_edges()] if coupling is not None else []
    )
    return normalize_properties(
        backend_name=backend.name,
        num_qubits=backend.num_qubits,
        coupling_map=coupling_edges,
        properties=properties,
    )

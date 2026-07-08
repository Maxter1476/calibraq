"""IBM normalization tested against real qiskit-ibm-runtime model classes.

Builds a synthetic ``BackendProperties`` using the same vendored classes the
live API returns (verified against qiskit-ibm-runtime 0.47.0), so the
normalizer is exercised without network access or credentials.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

qiskit_models = pytest.importorskip("qiskit_ibm_runtime.models")

from qiskit_ibm_runtime.models.backend_properties import GateProperties, Nduv

from app.collector.ibm import normalize_properties

T1_DATE = datetime(2026, 6, 10, 4, 0, tzinfo=timezone.utc)
READOUT_DATE = datetime(2026, 6, 10, 4, 25, tzinfo=timezone.utc)
LAST_UPDATE = datetime(2026, 6, 10, 5, 0, tzinfo=timezone.utc)


def _properties() -> object:
    """A two-qubit BackendProperties payload with mixed units."""
    qubit0 = [
        Nduv(T1_DATE, "T1", "us", 151.3),
        Nduv(T1_DATE, "T2", "us", 95.2),
        Nduv(T1_DATE, "frequency", "GHz", 4.97),
        Nduv(T1_DATE, "anharmonicity", "GHz", -0.331),
        Nduv(READOUT_DATE, "readout_error", "", 0.013),
        Nduv(READOUT_DATE, "operational", "", 1),
    ]
    # Qubit 1: different units (ms, MHz), a missing T2, and non-operational.
    qubit1 = [
        Nduv(T1_DATE, "T1", "ms", 0.1207),
        Nduv(T1_DATE, "frequency", "MHz", 5123.0),
        Nduv(READOUT_DATE, "readout_error", "", 0.5),
        Nduv(READOUT_DATE, "operational", "", 0),
    ]
    gates = [
        GateProperties(qubits=[0], gate="sx", parameters=[Nduv(READOUT_DATE, "gate_error", "", 2.1e-4)]),
        GateProperties(
            qubits=[0, 1],
            gate="ecr",
            parameters=[
                Nduv(T1_DATE, "gate_error", "", 6.8e-3),
                Nduv(T1_DATE, "gate_length", "ns", 460.0),
            ],
        ),
    ]
    return qiskit_models.BackendProperties(
        backend_name="fake_two_qubit",
        backend_version="1.0.0",
        last_update_date=LAST_UPDATE,
        qubits=[qubit0, qubit1],
        gates=gates,
        general=[],
    )


def test_normalize_properties() -> None:
    """Units convert, per-metric dates survive, and edges match either direction."""
    snapshot = normalize_properties(
        backend_name="fake_two_qubit",
        num_qubits=2,
        coupling_map=[(0, 1), (1, 0)],
        properties=_properties(),
    )

    assert snapshot.snapshot_source == "ibm"
    assert snapshot.timestamp == LAST_UPDATE

    q0, q1 = snapshot.qubits
    assert q0.t1_us == pytest.approx(151.3)
    assert q0.t1_calibrated_at == T1_DATE
    assert q0.readout_error_calibrated_at == READOUT_DATE  # differs from T1 date
    assert q0.sx_gate_error == pytest.approx(2.1e-4)
    assert q0.operational is True

    assert q1.t1_us == pytest.approx(120.7)  # ms -> us
    assert q1.frequency_ghz == pytest.approx(5.123)  # MHz -> GHz
    assert q1.t2_us is None  # missing metric stays missing
    assert q1.sx_gate_error is None  # no sx calibration for qubit 1
    assert q1.operational is False

    # Both directed edges resolve to the single calibrated ecr gate.
    assert len(snapshot.edges) == 2
    for edge in snapshot.edges:
        assert edge.gate_name == "ecr"
        assert edge.two_qubit_gate_error == pytest.approx(6.8e-3)
        assert edge.gate_duration_ns == pytest.approx(460.0)

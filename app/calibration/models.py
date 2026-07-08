"""Pydantic models describing a calibration snapshot of a quantum backend.

Design notes:

- Every metric carries its own ``*_calibrated_at`` timestamp. IBM calibrates
  metrics at different times (T1/T2 from one job, readout from another), and
  preserving the per-metric timestamp matters for drift analysis.
- Missing metrics are ``None`` — real calibration payloads are incomplete
  sometimes, and the model must round-trip that faithfully.
- All datetimes are normalized to tz-aware UTC by validators, so values coming
  back from SQLite (tz-naive, stored as UTC) compare equal to the originals.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, field_validator

from app.utils.timeutils import to_utc

#: Datetime fields shared by the validator on each model.
_QUBIT_TS_FIELDS = (
    "t1_calibrated_at",
    "t2_calibrated_at",
    "frequency_calibrated_at",
    "anharmonicity_calibrated_at",
    "readout_error_calibrated_at",
    "sx_gate_error_calibrated_at",
    "operational_calibrated_at",
)
_EDGE_TS_FIELDS = (
    "two_qubit_gate_error_calibrated_at",
    "gate_duration_calibrated_at",
)


class QubitMetrics(BaseModel):
    """Calibration metrics for one physical qubit at one snapshot."""

    model_config = ConfigDict(extra="forbid")

    qubit_index: int
    t1_us: float | None = None
    t1_calibrated_at: datetime | None = None
    t2_us: float | None = None
    t2_calibrated_at: datetime | None = None
    frequency_ghz: float | None = None
    frequency_calibrated_at: datetime | None = None
    anharmonicity_ghz: float | None = None
    anharmonicity_calibrated_at: datetime | None = None
    readout_error: float | None = None
    readout_error_calibrated_at: datetime | None = None
    sx_gate_error: float | None = None
    sx_gate_error_calibrated_at: datetime | None = None
    operational: bool = True
    operational_calibrated_at: datetime | None = None

    @field_validator(*_QUBIT_TS_FIELDS)
    @classmethod
    def _utc(cls, value: datetime | None) -> datetime | None:
        """Normalize timestamps to tz-aware UTC (naive values are assumed UTC)."""
        return to_utc(value)


class EdgeMetrics(BaseModel):
    """Two-qubit gate calibration metrics for one directed coupling edge."""

    model_config = ConfigDict(extra="forbid")

    q0: int
    q1: int
    gate_name: str  # typically "cz", "ecr", or "cx"
    two_qubit_gate_error: float | None = None
    two_qubit_gate_error_calibrated_at: datetime | None = None
    gate_duration_ns: float | None = None
    gate_duration_calibrated_at: datetime | None = None

    @field_validator(*_EDGE_TS_FIELDS)
    @classmethod
    def _utc(cls, value: datetime | None) -> datetime | None:
        """Normalize timestamps to tz-aware UTC (naive values are assumed UTC)."""
        return to_utc(value)


class CalibrationSnapshot(BaseModel):
    """One full calibration snapshot of a backend.

    ``timestamp`` is the calibration timestamp (UTC): for IBM data this is
    ``properties().last_update_date``; for mock data it is the simulated poll
    time. ``(backend_name, timestamp)`` is the idempotency key in storage.
    """

    model_config = ConfigDict(extra="forbid")

    backend_name: str
    timestamp: datetime
    num_qubits: int
    coupling_map: list[tuple[int, int]]
    qubits: list[QubitMetrics]
    edges: list[EdgeMetrics]
    snapshot_source: Literal["ibm", "mock"]

    @field_validator("timestamp")
    @classmethod
    def _utc(cls, value: datetime) -> datetime:
        """Normalize the snapshot timestamp to tz-aware UTC."""
        normalized = to_utc(value)
        assert normalized is not None
        return normalized

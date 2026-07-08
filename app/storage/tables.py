"""SQLAlchemy ORM tables for CalibraQ.

Schema (kept deliberately small):

- backends                : one row per known backend
- calibration_snapshots   : one row per (backend, calibration timestamp)
- qubit_metrics           : per-qubit metrics for a snapshot
- edge_metrics            : per-edge two-qubit gate metrics for a snapshot
- analysis_runs           : generic results store — drift, health, forecast,
                            and validation runs all land here (phase 2+)
- layout_recommendations  : layout selection outputs (phase 3)
- generated_reports       : rendered report artifacts (phase 4)

All datetimes are stored UTC tz-naive (SQLite convention); the Pydantic layer
re-attaches UTC on load.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from app.utils.timeutils import to_utc_naive, utcnow


def _utcnow_naive() -> datetime:
    """Default factory: current UTC time, tz-naive for SQLite storage."""
    naive = to_utc_naive(utcnow())
    assert naive is not None
    return naive


class Base(DeclarativeBase):
    """Declarative base for all CalibraQ tables."""


class Backend(Base):
    """A quantum backend we have seen at least one snapshot for."""

    __tablename__ = "backends"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(128), unique=True, nullable=False, index=True)
    num_qubits: Mapped[int] = mapped_column(Integer, nullable=False)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow_naive, nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow_naive, nullable=False)

    snapshots: Mapped[list["CalibrationSnapshotRow"]] = relationship(back_populates="backend")


class CalibrationSnapshotRow(Base):
    """One calibration snapshot; unique per (backend, calibration timestamp)."""

    __tablename__ = "calibration_snapshots"
    __table_args__ = (UniqueConstraint("backend_id", "snapshot_timestamp", name="uq_backend_caltime"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    backend_id: Mapped[int] = mapped_column(ForeignKey("backends.id"), nullable=False, index=True)
    snapshot_timestamp: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)
    collected_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow_naive, nullable=False)
    num_qubits: Mapped[int] = mapped_column(Integer, nullable=False)
    coupling_map_json: Mapped[str] = mapped_column(Text, nullable=False)
    snapshot_source: Mapped[str] = mapped_column(String(16), nullable=False)
    raw_json_path: Mapped[str | None] = mapped_column(String(512), nullable=True)

    backend: Mapped[Backend] = relationship(back_populates="snapshots")
    qubit_metrics: Mapped[list["QubitMetricRow"]] = relationship(
        back_populates="snapshot", cascade="all, delete-orphan", order_by="QubitMetricRow.qubit_index"
    )
    edge_metrics: Mapped[list["EdgeMetricRow"]] = relationship(
        back_populates="snapshot", cascade="all, delete-orphan", order_by="EdgeMetricRow.id"
    )


class QubitMetricRow(Base):
    """Per-qubit calibration metrics for one snapshot."""

    __tablename__ = "qubit_metrics"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    snapshot_id: Mapped[int] = mapped_column(
        ForeignKey("calibration_snapshots.id"), nullable=False, index=True
    )
    qubit_index: Mapped[int] = mapped_column(Integer, nullable=False)
    t1_us: Mapped[float | None] = mapped_column(Float, nullable=True)
    t1_calibrated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    t2_us: Mapped[float | None] = mapped_column(Float, nullable=True)
    t2_calibrated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    frequency_ghz: Mapped[float | None] = mapped_column(Float, nullable=True)
    frequency_calibrated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    anharmonicity_ghz: Mapped[float | None] = mapped_column(Float, nullable=True)
    anharmonicity_calibrated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    readout_error: Mapped[float | None] = mapped_column(Float, nullable=True)
    readout_error_calibrated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    sx_gate_error: Mapped[float | None] = mapped_column(Float, nullable=True)
    sx_gate_error_calibrated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    operational: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    operational_calibrated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    snapshot: Mapped[CalibrationSnapshotRow] = relationship(back_populates="qubit_metrics")


class EdgeMetricRow(Base):
    """Per-edge two-qubit gate metrics for one snapshot."""

    __tablename__ = "edge_metrics"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    snapshot_id: Mapped[int] = mapped_column(
        ForeignKey("calibration_snapshots.id"), nullable=False, index=True
    )
    q0: Mapped[int] = mapped_column(Integer, nullable=False)
    q1: Mapped[int] = mapped_column(Integer, nullable=False)
    gate_name: Mapped[str] = mapped_column(String(32), nullable=False)
    two_qubit_gate_error: Mapped[float | None] = mapped_column(Float, nullable=True)
    two_qubit_gate_error_calibrated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    gate_duration_ns: Mapped[float | None] = mapped_column(Float, nullable=True)
    gate_duration_calibrated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    snapshot: Mapped[CalibrationSnapshotRow] = relationship(back_populates="edge_metrics")


class AnalysisRun(Base):
    """Generic analysis result store.

    Drift, health, forecast, and validation runs (phases 2-3) all land here:
    ``run_type`` discriminates, ``params_json``/``results_json`` hold payloads.
    """

    __tablename__ = "analysis_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    params_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    results_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow_naive, nullable=False)


class LayoutRecommendation(Base):
    """A recommended qubit layout for a circuit on a backend (phase 3 output)."""

    __tablename__ = "layout_recommendations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    backend_id: Mapped[int] = mapped_column(ForeignKey("backends.id"), nullable=False, index=True)
    snapshot_id: Mapped[int | None] = mapped_column(
        ForeignKey("calibration_snapshots.id"), nullable=True
    )
    circuit_descriptor: Mapped[str] = mapped_column(Text, nullable=False)
    layout_json: Mapped[str] = mapped_column(Text, nullable=False)
    score: Mapped[float | None] = mapped_column(Float, nullable=True)
    method: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow_naive, nullable=False)


class GeneratedReport(Base):
    """A rendered report artifact (phase 4 output)."""

    __tablename__ = "generated_reports"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    report_type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    content_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    path: Mapped[str | None] = mapped_column(String(512), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow_naive, nullable=False)

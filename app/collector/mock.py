"""Mock calibration time-series generator.

**Mock data exists only so the pipeline can run without IBM credentials.**
It is labeled ``snapshot_source="mock"`` end to end, and all validation claims
in CalibraQ must come from real IBM data — never from this module.

Three synthetic backends:

- ``mock_stable_5q``    — 5 qubits, small noise, rare outages
- ``mock_drifting_5q``  — 5 qubits, errors degrade between recalibrations and
                          T1 has a slow long-term decline
- ``mock_unstable_16q`` — 16 qubits, heavy noise, frequent outages and
                          missing metrics

The series are deliberately messy, mimicking real calibration data:

- discontinuous jumps at recalibration events — T1 is re-measured and error
  baselines reset rather than drifting smoothly through the event
- qubits going non-operational for multi-poll windows
- T1 fluctuating non-monotonically day to day (lognormal noise on a baseline)
- occasional missing metrics (fields set to ``None``)
"""

from __future__ import annotations

import zlib
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import numpy as np

from app.calibration.models import CalibrationSnapshot, EdgeMetrics, QubitMetrics


def _line_coupling(n: int) -> list[tuple[int, int]]:
    """Directed line coupling map: i<->i+1 for each neighbor pair."""
    edges: list[tuple[int, int]] = []
    for i in range(n - 1):
        edges.append((i, i + 1))
        edges.append((i + 1, i))
    return edges


def _ring_coupling(n: int) -> list[tuple[int, int]]:
    """Directed ring coupling map."""
    edges: list[tuple[int, int]] = []
    for i in range(n):
        j = (i + 1) % n
        edges.append((i, j))
        edges.append((j, i))
    return edges


@dataclass(frozen=True)
class MockBackendSpec:
    """Static description of one synthetic backend."""

    name: str
    num_qubits: int
    coupling_map: list[tuple[int, int]]
    gate_name: str  # "cz" | "ecr" | "cx"
    noise_sigma: float  # lognormal sigma for day-to-day fluctuation
    outage_prob: float  # per qubit per poll chance of starting an outage
    missing_prob: float  # per metric per poll chance of being absent
    error_drift_per_poll: float  # multiplicative error growth between recalibrations
    t1_trend_per_poll: float  # multiplicative long-term T1 trend (1.0 = none)


MOCK_BACKEND_SPECS: tuple[MockBackendSpec, ...] = (
    MockBackendSpec(
        name="mock_stable_5q",
        num_qubits=5,
        coupling_map=_line_coupling(5),
        gate_name="cz",
        noise_sigma=0.05,
        outage_prob=0.004,
        missing_prob=0.01,
        error_drift_per_poll=1.005,
        t1_trend_per_poll=1.0,
    ),
    MockBackendSpec(
        name="mock_drifting_5q",
        num_qubits=5,
        coupling_map=_line_coupling(5),
        gate_name="ecr",
        noise_sigma=0.08,
        outage_prob=0.01,
        missing_prob=0.03,
        error_drift_per_poll=1.15,
        t1_trend_per_poll=0.997,
    ),
    MockBackendSpec(
        name="mock_unstable_16q",
        num_qubits=16,
        coupling_map=_ring_coupling(16),
        gate_name="cx",
        noise_sigma=0.25,
        outage_prob=0.04,
        missing_prob=0.08,
        error_drift_per_poll=1.04,
        t1_trend_per_poll=1.0,
    ),
)


@dataclass
class _QubitState:
    """Evolving hidden state for one mock qubit."""

    intrinsic_t1_us: float
    t1_baseline_us: float
    t2_factor: float
    frequency_ghz: float
    anharmonicity_ghz: float
    intrinsic_readout: float
    readout_baseline: float
    intrinsic_sx: float
    sx_baseline: float
    error_offset_min: float = 15.0
    outage_polls_left: int = 0
    polls_since_recal: int = 0


@dataclass
class _EdgeState:
    """Evolving hidden state for one mock coupling edge."""

    intrinsic_error: float
    error_baseline: float
    duration_ns: float
    polls_since_recal: int = 0


_GATE_DURATIONS_NS = {"cz": 68.0, "ecr": 460.0, "cx": 330.0}


def _init_qubit_state(rng: np.random.Generator) -> _QubitState:
    """Draw a fresh hidden state for a qubit."""
    intrinsic = float(rng.uniform(80.0, 200.0))
    intrinsic_readout = float(rng.uniform(0.005, 0.04))
    intrinsic_sx = float(10 ** rng.uniform(-4.0, -3.0))
    return _QubitState(
        intrinsic_t1_us=intrinsic,
        t1_baseline_us=intrinsic,
        t2_factor=float(rng.uniform(0.5, 1.3)),
        frequency_ghz=float(rng.uniform(4.6, 5.3)),
        anharmonicity_ghz=float(rng.uniform(-0.36, -0.30)),
        intrinsic_readout=intrinsic_readout,
        readout_baseline=intrinsic_readout,
        intrinsic_sx=intrinsic_sx,
        sx_baseline=intrinsic_sx,
        error_offset_min=float(rng.uniform(1, 45)),
    )


def _recalibrate_qubit(state: _QubitState, rng: np.random.Generator) -> None:
    """Apply a recalibration event: discontinuous reset, not a smooth drift.

    T1 is re-measured around the intrinsic value (a visible jump from wherever
    noise and drift had taken it). Error baselines reset *around the qubit's
    intrinsic values* — real recalibration restores a device to its
    characteristic error rates, it does not roll entirely new ones.
    """
    state.t1_baseline_us = state.intrinsic_t1_us * float(rng.lognormal(0.0, 0.18))
    state.readout_baseline = state.intrinsic_readout * float(rng.lognormal(0.0, 0.20))
    state.sx_baseline = state.intrinsic_sx * float(rng.lognormal(0.0, 0.20))
    state.error_offset_min = float(rng.uniform(1, 45))
    state.polls_since_recal = 0


def generate_history(
    spec: MockBackendSpec,
    days: int = 14,
    polls_per_day: int = 2,
    seed: int = 1234,
    end_time: datetime | None = None,
) -> list[CalibrationSnapshot]:
    """Generate a time series of mock snapshots for one synthetic backend.

    Snapshots are spaced ``24 / polls_per_day`` hours apart, ending at
    ``end_time`` (default: now, UTC). Deterministic for a given seed.
    """
    rng = np.random.default_rng(seed + zlib.crc32(spec.name.encode()) % 10_000)
    if end_time is None:
        end_time = datetime.now(timezone.utc).replace(microsecond=0)
    n_polls = days * polls_per_day
    interval = timedelta(hours=24.0 / polls_per_day)
    start_time = end_time - interval * (n_polls - 1)

    qubit_states = [_init_qubit_state(rng) for _ in range(spec.num_qubits)]
    edge_states = {}
    for q0, q1 in spec.coupling_map:
        if q0 < q1:
            intrinsic_error = float(10 ** rng.uniform(-2.6, -1.9))
            edge_states[(q0, q1)] = _EdgeState(
                intrinsic_error=intrinsic_error,
                error_baseline=intrinsic_error,
                duration_ns=_GATE_DURATIONS_NS[spec.gate_name] * float(rng.uniform(0.9, 1.15)),
            )
    last_recal_time = start_time - timedelta(hours=6)

    snapshots: list[CalibrationSnapshot] = []
    for poll in range(n_polls):
        poll_time = start_time + interval * poll

        # Roughly daily recalibration with jitter: probability per poll such
        # that one event per day is expected.
        recalibrated = rng.random() < (1.0 / polls_per_day) and poll > 0
        if recalibrated:
            for state in qubit_states:
                _recalibrate_qubit(state, rng)
            for edge_state in edge_states.values():
                edge_state.error_baseline = edge_state.intrinsic_error * float(
                    rng.lognormal(0.0, 0.20)
                )
                edge_state.polls_since_recal = 0
            last_recal_time = poll_time - timedelta(minutes=float(rng.uniform(5, 120)))

        qubits: list[QubitMetrics] = []
        for index, state in enumerate(qubit_states):
            state.polls_since_recal += 1
            state.intrinsic_t1_us *= spec.t1_trend_per_poll

            # Outage windows: qubit goes non-operational for several polls.
            if state.outage_polls_left > 0:
                state.outage_polls_left -= 1
                operational = False
            elif rng.random() < spec.outage_prob:
                state.outage_polls_left = int(rng.integers(1, 5))
                operational = False
            else:
                operational = True

            drift = spec.error_drift_per_poll**state.polls_since_recal
            t1 = state.t1_baseline_us * float(rng.lognormal(0.0, spec.noise_sigma))
            t2 = min(t1 * state.t2_factor, 2.0 * t1) * float(
                rng.lognormal(0.0, spec.noise_sigma)
            )
            readout = min(0.5, state.readout_baseline * drift * float(rng.lognormal(0.0, spec.noise_sigma)))
            sx = min(0.5, state.sx_baseline * drift * float(rng.lognormal(0.0, spec.noise_sigma)))

            # Metrics are calibrated at different times: T1/T2 at the last
            # recalibration, readout/sx slightly later. The offset is fixed
            # per calibration epoch (like real data: the calibrated_at stays
            # constant until the next recalibration).
            coherence_ts = last_recal_time
            error_ts = last_recal_time + timedelta(minutes=state.error_offset_min)

            def _maybe(value: float) -> float | None:
                return None if rng.random() < spec.missing_prob else value

            t1_v = _maybe(t1)
            t2_v = _maybe(t2)
            readout_v = _maybe(readout)
            sx_v = _maybe(sx)
            qubits.append(
                QubitMetrics(
                    qubit_index=index,
                    t1_us=t1_v,
                    t1_calibrated_at=coherence_ts if t1_v is not None else None,
                    t2_us=t2_v,
                    t2_calibrated_at=coherence_ts if t2_v is not None else None,
                    frequency_ghz=state.frequency_ghz,
                    frequency_calibrated_at=coherence_ts,
                    anharmonicity_ghz=state.anharmonicity_ghz,
                    anharmonicity_calibrated_at=coherence_ts,
                    readout_error=readout_v,
                    readout_error_calibrated_at=error_ts if readout_v is not None else None,
                    sx_gate_error=sx_v,
                    sx_gate_error_calibrated_at=error_ts if sx_v is not None else None,
                    operational=operational,
                    operational_calibrated_at=poll_time,
                )
            )

        edges: list[EdgeMetrics] = []
        for q0, q1 in spec.coupling_map:
            edge_state = edge_states[(min(q0, q1), max(q0, q1))]
            if q0 < q1:
                edge_state.polls_since_recal += 1
            drift = spec.error_drift_per_poll**edge_state.polls_since_recal
            error = min(
                0.9, edge_state.error_baseline * drift * float(rng.lognormal(0.0, spec.noise_sigma))
            )
            error_v = None if rng.random() < spec.missing_prob else error
            edges.append(
                EdgeMetrics(
                    q0=q0,
                    q1=q1,
                    gate_name=spec.gate_name,
                    two_qubit_gate_error=error_v,
                    two_qubit_gate_error_calibrated_at=(
                        last_recal_time if error_v is not None else None
                    ),
                    gate_duration_ns=edge_state.duration_ns,
                    gate_duration_calibrated_at=last_recal_time,
                )
            )

        snapshots.append(
            CalibrationSnapshot(
                backend_name=spec.name,
                timestamp=poll_time,
                num_qubits=spec.num_qubits,
                coupling_map=spec.coupling_map,
                qubits=qubits,
                edges=edges,
                snapshot_source="mock",
            )
        )
    return snapshots


def generate_all(
    days: int = 14,
    polls_per_day: int = 2,
    seed: int = 1234,
    end_time: datetime | None = None,
) -> dict[str, list[CalibrationSnapshot]]:
    """Generate mock histories for all three synthetic backends."""
    return {
        spec.name: generate_history(
            spec, days=days, polls_per_day=polls_per_day, seed=seed, end_time=end_time
        )
        for spec in MOCK_BACKEND_SPECS
    }

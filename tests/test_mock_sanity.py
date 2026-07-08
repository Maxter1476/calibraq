"""Statistical sanity of the mock generator: realistically messy, not clean."""

from __future__ import annotations

import numpy as np

from app.collector.mock import MOCK_BACKEND_SPECS, generate_all, generate_history

_SPEC_BY_NAME = {spec.name: spec for spec in MOCK_BACKEND_SPECS}


def _t1_series(snapshots: list, qubit_index: int) -> list[float]:
    """T1 time series for one qubit, skipping missing values."""
    return [
        snap.qubits[qubit_index].t1_us
        for snap in snapshots
        if snap.qubits[qubit_index].t1_us is not None
    ]


def test_recalibration_jumps_present() -> None:
    """Some consecutive T1 changes are large: discontinuous resets, not drift."""
    snapshots = generate_history(_SPEC_BY_NAME["mock_drifting_5q"], days=14, polls_per_day=2, seed=99)
    jump_found = False
    for q in range(5):
        series = np.array(_t1_series(snapshots, q))
        rel_change = np.abs(np.diff(series)) / series[:-1]
        if np.any(rel_change > 0.2):
            jump_found = True
            break
    assert jump_found, "expected at least one >20% T1 jump from a recalibration event"


def test_t1_non_monotonic() -> None:
    """T1 fluctuates day to day: diffs change sign, never one long slide."""
    for name in ("mock_stable_5q", "mock_drifting_5q", "mock_unstable_16q"):
        snapshots = generate_history(_SPEC_BY_NAME[name], days=14, polls_per_day=2, seed=3)
        diffs = np.diff(np.array(_t1_series(snapshots, 0)))
        assert np.any(diffs > 0) and np.any(diffs < 0), f"{name}: T1 series is monotonic"


def test_unstable_backend_has_outages_and_missing_metrics() -> None:
    """The 16q unstable backend shows non-operational windows and missing metrics."""
    snapshots = generate_history(_SPEC_BY_NAME["mock_unstable_16q"], days=14, polls_per_day=2, seed=11)
    flat = [q for snap in snapshots for q in snap.qubits]
    assert any(not q.operational for q in flat), "expected non-operational windows"
    assert any(q.t1_us is None for q in flat), "expected occasional missing T1"
    assert any(
        e.two_qubit_gate_error is None for snap in snapshots for e in snap.edges
    ), "expected occasional missing 2q gate error"


def test_mock_labeled_and_deterministic() -> None:
    """Everything is labeled mock, and a fixed seed reproduces the series."""
    from datetime import datetime, timezone

    end = datetime(2026, 6, 11, 12, 0, tzinfo=timezone.utc)
    all_a = generate_all(days=2, polls_per_day=2, seed=5, end_time=end)
    all_b = generate_all(days=2, polls_per_day=2, seed=5, end_time=end)
    for name, snaps in all_a.items():
        assert all(s.snapshot_source == "mock" for s in snaps)
        assert snaps == all_b[name], f"{name}: not deterministic for a fixed seed"

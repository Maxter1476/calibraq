"""Phase 3: drift-aware layout selection, validated against mapomatic.

mapomatic supplies the trusted baseline (vf2 layout matching + drift-blind
cost); CalibraQ adds a drift-aware cost function through mapomatic's extension
point and a comparison harness. All real-world validation claims must come
from real IBM data (see PROJECT_STATE.md).
"""

from app.layout.backend_shim import SnapshotBackend, snapshot_to_backend_properties
from app.layout.circuits import build_circuit, describe
from app.layout.runner import run_layout_selection
from app.layout.selection import (
    candidate_layouts,
    make_drift_aware_cost,
    score_drift_aware,
    score_static,
)
from app.layout.validation import LayoutComparison, compare_layouts

__all__ = [
    "LayoutComparison",
    "SnapshotBackend",
    "build_circuit",
    "candidate_layouts",
    "compare_layouts",
    "describe",
    "make_drift_aware_cost",
    "run_layout_selection",
    "score_drift_aware",
    "score_static",
    "snapshot_to_backend_properties",
]

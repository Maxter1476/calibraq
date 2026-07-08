#!/usr/bin/env bash
# Phase 3 smoke test: generate mock data, run drift-aware layout selection,
# verify it persists recommendations + a comparison, and cross-check that our
# drift-blind choice matches a direct, independent mapomatic call.
# Exits nonzero on any failure.
set -euo pipefail

cd "$(dirname "$0")/.."
PY="${PYTHON:-.venv/bin/python}"
if [ ! -x "$PY" ]; then PY=python3; fi

SMOKE_DIR="$(mktemp -d)"
trap 'rm -rf "$SMOKE_DIR"' EXIT
export SMOKE_DB="$SMOKE_DIR/smoke.db"

echo "== smoke: generating mock data =="
"$PY" scripts/generate_mock.py --days 8 --polls-per-day 2 --seed 7 \
  --db "$SMOKE_DB" --snapshot-dir "$SMOKE_DIR/snapshots"

echo "== smoke: selecting layouts (linear-4, drift-aware k=1.5) =="
"$PY" scripts/select_layout.py --db "$SMOKE_DB" --kind linear --qubits 4 \
  --risk-factor 1.5 --window-days 8

echo "== smoke: verifying persistence + mapomatic cross-check =="
"$PY" - <<'EOF'
import os
import sys

import mapomatic
from sqlalchemy import select

from app.calibration.loaders import load_snapshots
from app.layout.backend_shim import SnapshotBackend
from app.layout.circuits import build_circuit
from app.layout.validation import LayoutComparison
from app.storage.db import get_engine, session_scope
from app.storage.tables import AnalysisRun, LayoutRecommendation

engine = get_engine(os.environ["SMOKE_DB"])
with session_scope(engine) as session:
    runs = list(session.scalars(select(AnalysisRun).where(AnalysisRun.run_type == "layout_validation")))
    recs = list(session.scalars(select(LayoutRecommendation)))
    print(f"layout_validation runs={len(runs)} recommendations={len(recs)}")
    if len(runs) != 3:
        sys.exit("FAIL: expected 3 layout_validation runs (one per mock backend)")
    methods = {r.method for r in recs}
    if "mapomatic_static" not in methods or not any(m.startswith("drift_aware") for m in methods):
        sys.exit(f"FAIL: missing recommendation methods, got {methods}")

    # Cross-check against an independent mapomatic call for each backend.
    for run in runs:
        comp = LayoutComparison.model_validate_json(run.results_json)
        if not (-1.0 <= comp.kendall_tau <= 1.0):
            sys.exit(f"FAIL: kendall tau out of range for {comp.backend_name}")
        if comp.static_cost_delta < -1e-9:
            sys.exit("FAIL: drift choice beat static on static cost (impossible)")

        snaps = load_snapshots(session, backend_name=comp.backend_name)
        backend = SnapshotBackend(snaps[-1])
        qc = build_circuit("linear", 4, gate_name=snaps[-1].edges[0].gate_name)
        layouts = mapomatic.matching_layouts(qc, backend.coupling_map, strict_direction=False)
        independent = mapomatic.evaluate_layouts(qc, layouts, backend)
        if list(independent[0][0]) != comp.static_best_layout:
            sys.exit(
                f"FAIL: {comp.backend_name} drift-blind choice {comp.static_best_layout} "
                f"!= independent mapomatic best {independent[0][0]}"
            )
        tag = "same" if comp.same_choice else f"reranked->{comp.drift_best_layout}"
        print(f"  {comp.backend_name}: mapomatic best={comp.static_best_layout} cross-check OK; "
              f"drift {tag}; tau={comp.kendall_tau:.2f} src={comp.snapshot_source}")

print("mapomatic cross-check OK")
EOF

echo "SMOKE PHASE 3: PASS"

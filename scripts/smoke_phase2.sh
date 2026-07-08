#!/usr/bin/env bash
# Phase 2 smoke test: generate mock data, run drift + health analysis, verify
# analysis_runs rows and report sanity. Exits nonzero on any failure.
set -euo pipefail

cd "$(dirname "$0")/.."
PY="${PYTHON:-.venv/bin/python}"
if [ ! -x "$PY" ]; then PY=python3; fi

SMOKE_DIR="$(mktemp -d)"
trap 'rm -rf "$SMOKE_DIR"' EXIT
export SMOKE_DB="$SMOKE_DIR/smoke.db"

echo "== smoke: generating mock data =="
"$PY" scripts/generate_mock.py --days 10 --polls-per-day 2 --seed 7 \
  --db "$SMOKE_DB" --snapshot-dir "$SMOKE_DIR/snapshots"

echo "== smoke: running drift + health analysis =="
"$PY" scripts/run_analysis.py --db "$SMOKE_DB" --window-days 10

echo "== smoke: verifying analysis_runs =="
"$PY" - <<'EOF'
import json
import os
import sys

from sqlalchemy import select

from app.analysis.drift import BackendDriftReport
from app.analysis.health import BackendHealth
from app.storage.db import get_engine, session_scope
from app.storage.tables import AnalysisRun

engine = get_engine(os.environ["SMOKE_DB"])
with session_scope(engine) as session:
    runs = list(session.scalars(select(AnalysisRun)))
    drift_runs = [r for r in runs if r.run_type == "drift"]
    health_runs = [r for r in runs if r.run_type == "health"]
    print(f"analysis_runs: {len(drift_runs)} drift, {len(health_runs)} health")
    if len(drift_runs) != 3 or len(health_runs) != 3:
        sys.exit("FAIL: expected 3 drift + 3 health runs (one per mock backend)")

    health_by_backend = {}
    for run in health_runs:
        report = BackendHealth.model_validate_json(run.results_json)
        if not (0.0 <= report.health_score <= 100.0):
            sys.exit(f"FAIL: health score out of range for {report.backend_name}")
        health_by_backend[report.backend_name] = report.health_score
    for run in drift_runs:
        report = BackendDriftReport.model_validate_json(run.results_json)
        params = json.loads(run.params_json)
        if params["backend"] != report.backend_name:
            sys.exit("FAIL: drift run params/results backend mismatch")

    stable = health_by_backend["mock_stable_5q"]
    unstable = health_by_backend["mock_unstable_16q"]
    print(f"health: stable={stable:.1f} unstable={unstable:.1f}")
    if stable <= unstable:
        sys.exit("FAIL: stable mock backend should outscore the unstable one")
print("analysis verification OK")
EOF

echo "SMOKE PHASE 2: PASS"

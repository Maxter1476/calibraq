#!/usr/bin/env bash
# Phase 1 smoke test: generate mock data, load to SQLite, query it back,
# print row counts. Exits nonzero on any failure.
set -euo pipefail

cd "$(dirname "$0")/.."
PY="${PYTHON:-.venv/bin/python}"
if [ ! -x "$PY" ]; then PY=python3; fi

SMOKE_DIR="$(mktemp -d)"
trap 'rm -rf "$SMOKE_DIR"' EXIT
export SMOKE_DB="$SMOKE_DIR/smoke.db"

echo "== smoke: generating mock data into $SMOKE_DB =="
"$PY" scripts/generate_mock.py --days 5 --polls-per-day 2 --seed 7 \
  --db "$SMOKE_DB" --snapshot-dir "$SMOKE_DIR/snapshots"

echo "== smoke: querying it back =="
"$PY" - <<'EOF'
import os
import sys

from sqlalchemy import func, select

from app.calibration.loaders import load_snapshots
from app.storage.db import get_engine, session_scope
from app.storage.tables import Backend, CalibrationSnapshotRow, EdgeMetricRow, QubitMetricRow

engine = get_engine(os.environ["SMOKE_DB"])
with session_scope(engine) as session:
    backends = session.scalar(select(func.count(Backend.id))) or 0
    snapshots = session.scalar(select(func.count(CalibrationSnapshotRow.id))) or 0
    qubit_rows = session.scalar(select(func.count(QubitMetricRow.id))) or 0
    edge_rows = session.scalar(select(func.count(EdgeMetricRow.id))) or 0
    print(f"backends={backends} snapshots={snapshots} "
          f"qubit_metrics={qubit_rows} edge_metrics={edge_rows}")

    if backends != 3:
        sys.exit("FAIL: expected 3 mock backends")
    if snapshots != 30:  # 3 backends * 5 days * 2 polls/day
        sys.exit(f"FAIL: expected 30 snapshots, got {snapshots}")
    if qubit_rows == 0 or edge_rows == 0:
        sys.exit("FAIL: empty metric tables")

    # Round-trip one backend through the Pydantic layer.
    loaded = load_snapshots(session, backend_name="mock_unstable_16q")
    if len(loaded) != 10 or loaded[0].num_qubits != 16:
        sys.exit("FAIL: pydantic round-trip query returned unexpected data")
    if loaded[0].snapshot_source != "mock":
        sys.exit("FAIL: mock data not labeled as mock")
print("round-trip query OK")
EOF

echo "== smoke: raw JSON files =="
count=$(ls "$SMOKE_DIR/snapshots" | wc -l | tr -d ' ')
echo "raw json files: $count"
if [ "$count" -ne 30 ]; then
  echo "FAIL: expected 30 raw JSON snapshot files, got $count" >&2
  exit 1
fi

echo "SMOKE PHASE 1: PASS"

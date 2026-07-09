# CalibraQ

![CI](https://github.com/Maxter1476/calibraq/actions/workflows/ci.yml/badge.svg)

**Drift-aware qubit selection, validated against mapomatic on real IBM
calibration data.**

CalibraQ polls IBM Quantum backend calibration data on a schedule, preserves
per-metric calibration timestamps, and stores everything in SQLite — the data
foundation for drift detection, health scoring, and drift-aware layout
selection in later phases. See `PROJECT_STATE.md` for the phase plan and
current status.

## Install

```bash
cd calibraq
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
cp .env.example .env   # add your IBM_QUANTUM_TOKEN
```

## Run

```bash
# Collect real IBM calibration data (cron-safe; exits 0 without a token)
.venv/bin/python scripts/collect_ibm.py

# Or generate mock data so the pipeline runs without credentials
# (mock data is labeled snapshot_source="mock" and never backs validation claims)
.venv/bin/python scripts/generate_mock.py --days 14 --polls-per-day 2

# Drift + health analysis over stored snapshots (results -> analysis_runs)
.venv/bin/python scripts/run_analysis.py --window-days 14

# Drift-aware layout selection vs. mapomatic baseline (-> layout_recommendations)
# risk-factor 0 reproduces mapomatic exactly; higher values penalize drift.
.venv/bin/python scripts/select_layout.py --kind linear --qubits 4 --risk-factor 1.0

# Tests and smoke
.venv/bin/python -m pytest
./scripts/smoke_phase1.sh
./scripts/smoke_phase2.sh
./scripts/smoke_phase3.sh
```

Cron example (2x daily):

```cron
0 6,18 * * * cd /path/to/calibraq && .venv/bin/python scripts/collect_ibm.py >> data/collect.log 2>&1
```

## Layout

```
app/calibration/   Pydantic models + ORM<->Pydantic loaders
app/storage/       SQLAlchemy engine, session, ORM tables (SQLite)
app/collector/     IBM polling + mock generation + persistence
app/analysis/      recalibration-aware drift detection + health scoring
app/layout/        mapomatic bridge + drift-aware layout selection + comparison
data/snapshots/    raw JSON, one file per backend per poll
scripts/           collect_ibm.py, generate_mock.py, run_analysis.py,
                   select_layout.py, smoke_phase{1,2,3}.sh
```

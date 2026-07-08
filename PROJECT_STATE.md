# PROJECT_STATE — Phase 3 (layout + validation machinery) complete

Updated: 2026-06-15. Phases 1 (data), 2 (drift + health), and 3 (drift-aware
layout selection + mapomatic comparison machinery) are done. The phase-3
*predictive* validation claim is explicitly deferred to real data (below).
Next is phase 4 (API + dashboard).

## What works

### Phase 1 — data foundation
- Pydantic v2 `CalibrationSnapshot`/`QubitMetrics`/`EdgeMetrics` with per-metric
  `*_calibrated_at`; missing metrics `None`; lossless round-trip.
- SQLAlchemy 2.x + SQLite; UTC-naive storage, Pydantic re-attaches UTC; insert
  idempotent on `(backend, calibration timestamp)`.
- IBM collector verified against qiskit-ibm-runtime **0.47.0** (pinned),
  cron-safe (always exits 0), raw JSON + SQLite, one line per backend.
- Mock generator: 5q stable / 5q drifting / 16q unstable, deterministic,
  realistically messy; labeled `snapshot_source="mock"`.

### Phase 2 — drift + health
- Epoch model (`app/analysis/series.py`): recalibration events from
  `calibrated_at` changes, never value jumps.
- Drift (`app/analysis/drift.py`): across-epoch volatility, secular trend/day,
  within-epoch growth; per metric per qubit/edge; persisted to `analysis_runs`.
- Health (`app/analysis/health.py`): 0-100 per-qubit composite with missing
  components renormalized; backend score adds 2q error + operational fraction.
- `scripts/run_analysis.py` runs both and persists them. Mock profiles rank
  stable < drifting < unstable on drift across all tested seeds.

### Phase 3 — layout + validation machinery (new)
- **mapomatic is the trusted baseline, used directly** (pinned `~=0.14.0`,
  verified by source introspection): `matching_layouts` (vf2 enumeration) and
  `evaluate_layouts` + `default_cost` (drift-blind expected infidelity).
- **Backend bridge** (`app/layout/backend_shim.py`):
  `snapshot_to_backend_properties` reconstructs a real qiskit `BackendProperties`
  from a stored snapshot (readout + sx/x + two-qubit gate errors, 2q gates
  mirrored to both directed orderings); `SnapshotBackend` wraps it with the
  minimal `configuration().basis_gates` / `properties()` / `coupling_map`
  surface mapomatic touches. Missing metrics fall back to 0.5 (penalized, never
  treated as ideal). Verified: a hand-computed circuit infidelity matches
  mapomatic's cost through the shim to 1e-9.
- **Drift-aware cost** (`app/layout/selection.py`): a mapomatic-compatible
  `cost_function` that replaces each resource error `e` with
  `e * (1 + risk_factor * drift_score)` using the phase-2 drift report. With
  `risk_factor=0` it equals `default_cost` exactly (tested to 1e-12, and shown
  at the CLI: `--risk-factor 0` gives `tau=1.00`, same layout, on all backends).
- **Benchmark circuits** (`app/layout/circuits.py`): linear / ring / star,
  built directly in an IBM basis with the backend's native 2q gate name.
- **Comparison harness** (`app/layout/validation.py`): `LayoutComparison`
  reports candidate count, both top choices, whether they agree, the drift
  choice's rank in the static ranking, the static-cost trade-off and
  drift-exposure reduction of switching, Kendall tau-b rank correlation, and
  top-3 overlap.
- **Runner + CLI** (`app/layout/runner.py`, `scripts/select_layout.py`):
  persists both chosen layouts to `layout_recommendations` (methods
  `mapomatic_static` and `drift_aware_k<k>`) and the full comparison to
  `analysis_runs` (`run_type="layout_validation"`).
- **Cross-check** (`scripts/smoke_phase3.sh`): independently calls mapomatic and
  asserts our stored drift-blind choice equals mapomatic's own top layout for
  every backend — this is the literal "validated against mapomatic" check.
- **Tests**: 23 total (5 new in `tests/test_layout.py`): shim→mapomatic exact
  cost, `k=0` == baseline, drift penalty reranks toward low-drift edges,
  end-to-end persistence + parse-back, drift-exposure monotonicity.

## Stubbed / Cut

- **The phase-3 predictive validation claim is NOT made.** Everything here runs
  on mock data and is *descriptive*: it shows the machinery is correct and
  characterizes how drift-aware reranks vs. mapomatic. Whether drift-aware
  selection yields better real-world fidelity requires (a) real IBM calibration
  history and (b) running chosen layouts on hardware at job time and measuring
  fidelity. Per the project rule, that claim may only come from real data.
  The harness is real-data-ready: point it at `snapshot_source="ibm"` rows and
  it runs unchanged (the comparison records the source).
- The IBM collector still has not run against a live account (machine's saved
  account uses the retired `ibm_quantum` channel; needs an IBM Cloud API key).
- `risk_factor` (drift penalty `k`, default 1.0) is a heuristic knob, like the
  phase-2 scales; not fitted to real outcomes.
- Drift-aware cost inflates errors by drift but does not yet model calibration
  *age* at job time (how stale the latest snapshot is when a job runs). A
  forecast term (expected error given calibration age) is a candidate for
  phase 3.5 / 4 but is not built.
- Layout matching uses `strict_direction=False`; we mirror 2q errors to both
  directions, so directional gate asymmetry (real on ecr/cx hardware) is not
  distinguished. Revisit on real data.
- `generated_reports` table still has no writers (phase 4).
- Circuit set is the three built-in topologies; no arbitrary user circuits /
  QASM input yet.

## Commands

```bash
.venv/bin/pip install -e ".[dev]"                                  # install (Python 3.11+)
.venv/bin/python scripts/collect_ibm.py                            # real collection (token in .env)
.venv/bin/python scripts/generate_mock.py                          # mock data
.venv/bin/python scripts/run_analysis.py                           # drift + health
.venv/bin/python scripts/select_layout.py --kind linear --qubits 4 # layout selection vs mapomatic
.venv/bin/python -m compileall .                                   # phase-end checks
.venv/bin/python -m pytest                                         # 23 tests
./scripts/smoke_phase1.sh && ./scripts/smoke_phase2.sh && ./scripts/smoke_phase3.sh
```

## Schema summary

- `backends`, `calibration_snapshots` (UNIQUE `(backend_id, snapshot_timestamp)`),
  `qubit_metrics`, `edge_metrics` — phase 1.
- `analysis_runs(id, run_type, params_json, results_json, created_at)` —
  `run_type` in {`drift`, `health`, `layout_validation`}; holds the respective
  report JSON.
- `layout_recommendations(id, backend_id FK, snapshot_id FK, circuit_descriptor,
  layout_json, score, method, created_at)` — populated by phase 3
  (`method` = `mapomatic_static` | `drift_aware_k<k>`).
- `generated_reports` — schema only, no writers yet.

## Open questions for phase 4

1. **What does the API serve?** Latest health per backend, drift flags, and the
   current layout recommendation for a given circuit are the obvious endpoints.
   Read-only over the SQLite store, or a thin compute-on-request layer?
2. **Real-data validation loop** (the actual phase-3 claim): once credentials
   land, define the protocol — collect ~2-3 weeks history, submit drift-blind
   vs. drift-aware layouts for a fixed circuit set, measure fidelity (e.g. via
   a known-answer benchmark), and report whether drift-aware wins. Pre-register
   the success metric before running so it is not post-hoc.
3. **Calibration-age forecasting**: should the drift-aware cost incorporate the
   age of the latest calibration at job-submit time (stale calibration ->
   inflate more), turning drift score into a time-to-run risk estimate?
4. **Dashboard scope**: drift/health time series per qubit, recalibration event
   markers (from `calibrated_at`), and layout recommendation diffs over time.
5. **Re-fit heuristics on real data**: `VOLATILITY_SCALE`, health references,
   and `risk_factor` are all mock-calibrated; schedule a re-fit once a few weeks
   of real snapshots exist.

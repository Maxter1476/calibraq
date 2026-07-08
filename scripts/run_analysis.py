"""Run drift + health analysis on stored snapshots and persist the results.

For each backend in the database (or one named via ``--backend``), computes
the recalibration-aware drift report and the health report over the window,
stores both in ``analysis_runs`` (run_type ``drift`` / ``health``), and prints
a one-line summary per backend.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select

from app.analysis.drift import run_drift_analysis
from app.analysis.health import run_health_analysis
from app.storage.db import get_engine, init_db, session_scope
from app.storage.tables import Backend


def main(argv: list[str] | None = None) -> int:
    """Run drift + health for stored backends; nonzero exit on failure."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backend", type=str, default=None, help="one backend (default: all)")
    parser.add_argument("--window-days", type=float, default=14.0, help="analysis window (default 14)")
    parser.add_argument("--db", type=str, default=None, help="SQLite path (default data/calibraq.db)")
    args = parser.parse_args(argv)

    engine = get_engine(args.db)
    init_db(engine)

    with session_scope(engine) as session:
        names = (
            [args.backend]
            if args.backend
            else list(session.scalars(select(Backend.name).order_by(Backend.name)))
        )
        if not names:
            print("run_analysis: no backends stored; run collect_ibm.py or generate_mock.py first.")
            return 1

        for name in names:
            drift_run_id, drift = run_drift_analysis(session, name, window_days=args.window_days)
            health_run_id, health = run_health_analysis(
                session, name, window_days=args.window_days, drift_report=drift
            )
            worst = min(health.qubits, key=lambda q: q.health_score, default=None)
            worst_desc = (
                f"worst q{worst.qubit_index}={worst.health_score:.1f}" if worst else "no qubits"
            )
            flagged = sum(1 for q in drift.qubits for m in q.metrics if m.flagged)
            print(
                f"{name}: health={health.health_score:.1f} "
                f"drift={drift.backend_drift_score:.3f} ({flagged} flagged metric series) "
                f"{worst_desc} uptime={health.operational_fraction:.2%} "
                f"[runs {drift_run_id},{health_run_id}]"
            )
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Select circuit layouts for stored backends and compare against mapomatic.

For each backend (or one named via ``--backend``), builds a benchmark circuit
matching the backend's native two-qubit gate, ranks layouts both with
mapomatic's drift-blind cost and CalibraQ's drift-aware cost, persists the
chosen layouts and the comparison, and prints a one-line summary.

The summary describes how the two scorers differ on stored data. It is NOT a
claim that drift-aware selection performs better in practice; that requires
real hardware runs on real calibration data (see PROJECT_STATE.md).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select

from app.layout.runner import run_layout_selection
from app.storage.db import get_engine, init_db, session_scope
from app.storage.tables import Backend


def main(argv: list[str] | None = None) -> int:
    """Run layout selection for stored backends; nonzero exit on failure."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backend", type=str, default=None, help="one backend (default: all)")
    parser.add_argument("--kind", type=str, default="linear", help="linear|ring|star (default linear)")
    parser.add_argument("--qubits", type=int, default=3, help="circuit qubit count (default 3)")
    parser.add_argument("--risk-factor", type=float, default=1.0, help="drift risk factor k (default 1.0)")
    parser.add_argument("--window-days", type=float, default=14.0, help="drift window (default 14)")
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
            print("select_layout: no backends stored; run collect_ibm.py or generate_mock.py first.")
            return 1

        for name in names:
            try:
                comp, ids = run_layout_selection(
                    session,
                    name,
                    kind=args.kind,
                    n_qubits=args.qubits,
                    risk_factor=args.risk_factor,
                    window_days=args.window_days,
                )
            except ValueError as exc:
                print(f"{name}: skipped ({exc})")
                continue
            choice = "same layout" if comp.same_choice else (
                f"drift picks {comp.drift_best_layout} "
                f"(static rank #{comp.drift_best_rank_in_static + 1})"
            )
            print(
                f"{name} [{comp.circuit_descriptor}, src={comp.snapshot_source}]: "
                f"{comp.n_candidates} candidates, mapomatic best={comp.static_best_layout} "
                f"cost={comp.static_best_cost:.4f}; {choice}; "
                f"tau={comp.kendall_tau:.2f} "
                f"[runs val={ids['validation']} recs={ids['static']},{ids['drift_aware']}]"
            )
    return 0


if __name__ == "__main__":
    sys.exit(main())

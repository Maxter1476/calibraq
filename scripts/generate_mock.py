"""Generate mock calibration history and load it into SQLite.

Mock data exists only so the pipeline runs without IBM credentials; it is
labeled ``snapshot_source="mock"`` and must never back validation claims.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.collector.mock import generate_all
from app.collector.persist import DEFAULT_SNAPSHOT_DIR, persist_snapshot
from app.storage.db import get_engine, init_db, session_scope


def main(argv: list[str] | None = None) -> int:
    """Generate mock histories, write raw JSON, and insert into SQLite."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=int, default=14, help="days of history (default 14)")
    parser.add_argument("--polls-per-day", type=int, default=2, help="polls per day (default 2)")
    parser.add_argument("--seed", type=int, default=1234, help="RNG seed (default 1234)")
    parser.add_argument("--db", type=str, default=None, help="SQLite path (default data/calibraq.db)")
    parser.add_argument(
        "--snapshot-dir", type=str, default=str(DEFAULT_SNAPSHOT_DIR),
        help="raw JSON output dir (default data/snapshots)",
    )
    args = parser.parse_args(argv)

    engine = get_engine(args.db)
    init_db(engine)

    histories = generate_all(days=args.days, polls_per_day=args.polls_per_day, seed=args.seed)
    for backend_name, snapshots in histories.items():
        inserted = 0
        skipped = 0
        with session_scope(engine) as session:
            for snapshot in snapshots:
                _, was_inserted = persist_snapshot(session, snapshot, args.snapshot_dir)
                if was_inserted:
                    inserted += 1
                else:
                    skipped += 1
        print(f"{backend_name}: {len(snapshots)} snapshots ({inserted} inserted, {skipped} skipped)")
    return 0


if __name__ == "__main__":
    sys.exit(main())

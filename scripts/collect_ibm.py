"""Poll IBM Quantum backends and store calibration snapshots.

Designed to run 1-4x daily via cron, e.g.::

    0 6,18 * * *  cd /path/to/calibraq && .venv/bin/python scripts/collect_ibm.py

Reads ``IBM_QUANTUM_TOKEN`` (and optional ``IBM_QUANTUM_INSTANCE``) from the
environment / ``.env``. Idempotent on (backend, calibration timestamp).

This script must never crash the cron: missing token, network failure, or a
bad backend logs clearly and exits 0.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# Allow running as `python scripts/collect_ibm.py` from the repo root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv

from app.collector.ibm import snapshot_from_backend
from app.collector.persist import DEFAULT_SNAPSHOT_DIR, persist_snapshot
from app.storage.db import get_engine, init_db, session_scope


def _connect() -> "object | None":
    """Build a QiskitRuntimeService from env token or saved account, else None.

    Prefers ``IBM_QUANTUM_TOKEN`` from the environment / ``.env``; falls back
    to a saved account (``~/.qiskit/qiskit-ibm.json``). Never raises.
    """
    from qiskit_ibm_runtime import QiskitRuntimeService

    token = os.getenv("IBM_QUANTUM_TOKEN")
    if token:
        try:
            return QiskitRuntimeService(
                channel="ibm_quantum_platform",
                token=token,
                instance=os.getenv("IBM_QUANTUM_INSTANCE") or None,
            )
        except Exception as exc:
            print(f"collect_ibm: token auth failed ({type(exc).__name__}: {exc}).")
            return None
    try:
        return QiskitRuntimeService()
    except Exception as exc:
        print(
            "collect_ibm: IBM_QUANTUM_TOKEN not set (see .env.example) and no usable "
            f"saved account ({type(exc).__name__}: {exc}); nothing collected."
        )
        return None


def main() -> int:
    """Collect one snapshot per available IBM backend. Always returns 0."""
    load_dotenv()
    service = _connect()
    if service is None:
        return 0
    try:
        backends = service.backends()
    except Exception as exc:  # cron safety: any network failure exits 0
        print(f"collect_ibm: could not list backends ({type(exc).__name__}: {exc}); "
              "nothing collected.")
        return 0

    engine = get_engine()
    init_db(engine)
    snapshot_dir = os.getenv("CALIBRAQ_SNAPSHOT_DIR", str(DEFAULT_SNAPSHOT_DIR))

    collected = 0
    for backend in backends:
        name = getattr(backend, "name", "<unknown>")
        try:
            snapshot = snapshot_from_backend(backend)
            if snapshot is None:
                print(f"{name}: skipped (simulator or no calibration properties)")
                continue
            with session_scope(engine) as session:
                path, inserted = persist_snapshot(session, snapshot, snapshot_dir)
            status = "inserted" if inserted else "already stored (same calibration)"
            print(
                f"{name}: {snapshot.num_qubits}q, calibrated {snapshot.timestamp:%Y-%m-%d %H:%M}Z, "
                f"{status}, raw={path}"
            )
            collected += 1
        except Exception as exc:  # one bad backend must not kill the poll
            print(f"{name}: ERROR {type(exc).__name__}: {exc}")
    print(f"collect_ibm: done, {collected} backend(s) processed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

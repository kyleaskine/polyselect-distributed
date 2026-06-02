"""poly-server CLI:  init | extend | serve | prune   (DESIGN.md §4, §8, §11).

init/extend/prune are run on the droplet over SSH. serve runs the worker API.

    python3 -m polyserver init   --jobdir DIR --worktodo worktodo.ini \
                                 --coeff-list coeff_list.txt --high-coeff-mult 60060 [--force]
    python3 -m polyserver serve  --jobdir DIR --bind 0.0.0.0 --port 8080
    python3 -m polyserver extend --jobdir DIR --coeff-list more_coeffs.txt
    python3 -m polyserver prune  --jobdir DIR --confirm
"""
from __future__ import annotations

import argparse
import os
import pathlib
import secrets
import shutil
import sqlite3

from . import db


def _read_coeff_list(path: str) -> list[str]:
    out: list[str] = []
    for line in pathlib.Path(path).read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        out.append(line)
    return out


def _validate_coeffs(coeffs: list[str]) -> None:
    bad = [c for c in coeffs if not c.isdigit()]  # positive decimal, no sign/space/punctuation
    if bad:
        raise SystemExit(f"error: {len(bad)} non-decimal coefficient(s), e.g. {bad[:3]}")
    if not coeffs:
        raise SystemExit("error: coefficient list is empty")


def _existing_workunits(db_path: pathlib.Path) -> int:
    if not db_path.exists():
        return 0
    conn = db.connect(str(db_path))
    try:
        return conn.execute("SELECT COUNT(*) FROM workunits").fetchone()[0]
    except sqlite3.OperationalError:
        return 0  # db exists but schema not initialized
    finally:
        conn.close()


def cmd_init(a):
    jobdir = pathlib.Path(a.jobdir)
    db_path = jobdir / "job.db"

    existing = _existing_workunits(db_path)
    if existing and not a.force:
        raise SystemExit(
            f"error: {db_path} already has {existing} workunits — refusing to mix a new N "
            f"with an existing job. Pass --force to wipe the DB and polys/ and reinit.")
    if a.force and db_path.exists():
        for p in (db_path, jobdir / "job.db-wal", jobdir / "job.db-shm"):
            p.unlink(missing_ok=True)
        shutil.rmtree(jobdir / "polys", ignore_errors=True)
        print("--force: wiped existing job.db and polys/")

    jobdir.mkdir(parents=True, exist_ok=True)
    conn = db.connect(str(db_path))
    db.init_schema(conn)

    coeffs = _read_coeff_list(a.coeff_list)
    _validate_coeffs(coeffs)
    token = a.worker_token or secrets.token_hex(32)
    n = pathlib.Path(a.worktodo).read_text().strip() if a.worktodo else a.n
    if not n.isdigit():
        raise SystemExit("error: N must be a bare decimal integer (worktodo.ini format)")

    added = db.init_job(
        conn, n=n, degree=a.degree, high_coeff_mult=a.high_coeff_mult,
        deadline=a.deadline, collengine=a.collengine, worker_token=token, coeffs=coeffs,
    )
    token_path = jobdir / "token"
    token_path.write_text(token)
    os.chmod(token_path, 0o600)
    print(f"initialized job in {jobdir} with {added} coefficients (N has {len(n)} digits)")
    print(f"worker token -> {token_path} (chmod 600)")


def cmd_extend(a):
    conn = db.connect(str(pathlib.Path(a.jobdir) / "job.db"))
    coeffs = _read_coeff_list(a.coeff_list)
    _validate_coeffs(coeffs)
    print(f"added {db.extend_job(conn, coeffs)} coefficients")


def cmd_serve(a):
    import uvicorn

    from .app import create_app

    jobdir = pathlib.Path(a.jobdir)
    conn = db.connect(str(jobdir / "job.db"))
    if a.bind == "0.0.0.0":
        print("WARNING: --bind 0.0.0.0 exposes the worker API on all interfaces. Keep it on a "
              "private network / firewall; admin ops stay on SSH (DESIGN.md §8).")
    app = create_app(conn, jobdir=str(jobdir), lease_seconds=a.lease_seconds,
                     spotcheck_k=a.spotcheck_k)
    # Single-process by design (one shared SQLite connection + in-process lock).
    # Do NOT pass workers>1 — it would break that invariant.
    uvicorn.run(app, host=a.bind, port=a.port)


def cmd_prune(a):
    # Phase 2: delete blobs marked archived (pulled + sha-verified). Dry-run unless --confirm.
    raise SystemExit("prune: Phase 2 TODO (see DESIGN.md §11)")


def main(argv=None):
    p = argparse.ArgumentParser(prog="poly-server")
    sub = p.add_subparsers(dest="cmd", required=True)

    pi = sub.add_parser("init", help="create a job from an explicit coefficient list")
    pi.set_defaults(fn=cmd_init)
    pi.add_argument("--jobdir", required=True)
    pi.add_argument("--coeff-list", required=True, dest="coeff_list",
                    help="one positive-decimal leading coefficient per line (e.g. coeff_list.txt)")
    g = pi.add_mutually_exclusive_group(required=True)
    g.add_argument("--worktodo", help="path to worktodo.ini (bare decimal N)")
    g.add_argument("--n", help="the composite N as a decimal string")
    pi.add_argument("--degree", type=int, default=5)
    pi.add_argument("--high-coeff-mult", type=int, required=True, dest="high_coeff_mult")
    pi.add_argument("--deadline", type=int, default=8640000, help="CPU-seconds per coeff")
    pi.add_argument("--collengine", default="gerbicz")
    pi.add_argument("--worker-token", dest="worker_token", default=None,
                    help="reuse a token; default generates a fresh one")
    pi.add_argument("--force", action="store_true",
                    help="wipe an existing job.db + polys/ before reinit")

    pe = sub.add_parser("extend", help="append more coefficients to a running job")
    pe.set_defaults(fn=cmd_extend)
    pe.add_argument("--jobdir", required=True)
    pe.add_argument("--coeff-list", required=True, dest="coeff_list")

    ps = sub.add_parser("serve", help="run the worker API (single process by design)")
    ps.set_defaults(fn=cmd_serve)
    ps.add_argument("--jobdir", required=True)
    ps.add_argument("--bind", default="127.0.0.1")
    ps.add_argument("--port", type=int, default=8080)
    ps.add_argument("--lease-seconds", type=int, default=3600, dest="lease_seconds")
    ps.add_argument("--spotcheck-k", type=int, default=50, dest="spotcheck_k",
                    help="sampled records to structurally verify per submit (0 disables)")

    pp = sub.add_parser("prune", help="delete pulled+verified blobs (Phase 2)")
    pp.set_defaults(fn=cmd_prune)
    pp.add_argument("--jobdir", required=True)
    pp.add_argument("--confirm", action="store_true")

    a = p.parse_args(argv)
    a.fn(a)


if __name__ == "__main__":
    main()

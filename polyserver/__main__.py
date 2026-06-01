"""poly-server CLI:  init | extend | serve | prune   (DESIGN.md §4, §8, §11).

init/extend/prune are run on the droplet over SSH. serve runs the worker API.

    python3 -m polyserver init   --jobdir DIR --worktodo worktodo.ini \
                                 --coeff-list coeff_list.txt --high-coeff-mult 60060
    python3 -m polyserver serve  --jobdir DIR --bind 0.0.0.0 --port 8080
    python3 -m polyserver extend --jobdir DIR --coeff-list more_coeffs.txt
    python3 -m polyserver prune  --jobdir DIR --confirm
"""
from __future__ import annotations

import argparse
import os
import pathlib
import secrets

from . import db


def _read_coeff_list(path: str) -> list[str]:
    out: list[str] = []
    for line in pathlib.Path(path).read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        out.append(line)
    return out


def cmd_init(a):
    jobdir = pathlib.Path(a.jobdir)
    jobdir.mkdir(parents=True, exist_ok=True)
    conn = db.connect(str(jobdir / "job.db"))
    db.init_schema(conn)

    coeffs = _read_coeff_list(a.coeff_list)
    token = a.worker_token or secrets.token_hex(32)
    n = pathlib.Path(a.worktodo).read_text().strip() if a.worktodo else a.n

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
    print(f"added {db.extend_job(conn, _read_coeff_list(a.coeff_list))} coefficients")


def cmd_serve(a):
    import uvicorn

    from .app import create_app

    jobdir = pathlib.Path(a.jobdir)
    conn = db.connect(str(jobdir / "job.db"))
    app = create_app(conn, jobdir=str(jobdir), lease_seconds=a.lease_seconds)
    # NB: --bind 0.0.0.0 only when the coordinator should be reachable off-box.
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
                    help="one leading coefficient per line (e.g. coeff_list.txt)")
    g = pi.add_mutually_exclusive_group(required=True)
    g.add_argument("--worktodo", help="path to worktodo.ini (bare decimal N)")
    g.add_argument("--n", help="the composite N as a decimal string")
    pi.add_argument("--degree", type=int, default=5)
    pi.add_argument("--high-coeff-mult", type=int, required=True, dest="high_coeff_mult")
    pi.add_argument("--deadline", type=int, default=8640000, help="CPU-seconds per coeff")
    pi.add_argument("--collengine", default="gerbicz")
    pi.add_argument("--worker-token", dest="worker_token", default=None,
                    help="reuse a token; default generates a fresh one")

    pe = sub.add_parser("extend", help="append more coefficients to a running job")
    pe.set_defaults(fn=cmd_extend)
    pe.add_argument("--jobdir", required=True)
    pe.add_argument("--coeff-list", required=True, dest="coeff_list")

    ps = sub.add_parser("serve", help="run the worker API")
    ps.set_defaults(fn=cmd_serve)
    ps.add_argument("--jobdir", required=True)
    ps.add_argument("--bind", default="127.0.0.1")
    ps.add_argument("--port", type=int, default=8080)
    ps.add_argument("--lease-seconds", type=int, default=3600, dest="lease_seconds")

    pp = sub.add_parser("prune", help="delete pulled+verified blobs (Phase 2)")
    pp.set_defaults(fn=cmd_prune)
    pp.add_argument("--jobdir", required=True)
    pp.add_argument("--confirm", action="store_true")

    a = p.parse_args(argv)
    a.fn(a)


if __name__ == "__main__":
    main()

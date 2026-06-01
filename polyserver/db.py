"""SQLite metadata store for poly-server.

Metadata only — raw .ms polynomial blobs live on disk under <jobdir>/polys/,
content-addressed and zstd-compressed (DESIGN.md §9). One job per server.

Threading: the FastAPI event loop owns one connection; the background verifier
(Phase 2) opens its own. WAL + busy_timeout keep them from blocking. If you add
another DB-touching task, follow the same pattern (its own connection).
"""
from __future__ import annotations

import pathlib
import sqlite3
import time

SCHEMA_PATH = pathlib.Path(__file__).resolve().parent.parent / "schema.sql"


def connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, isolation_level=None)  # autocommit; we manage txns
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA_PATH.read_text())


# --------------------------------------------------------------------------- #
# Admin (run on the droplet over SSH — DESIGN.md §8)
# --------------------------------------------------------------------------- #

def init_job(conn, *, n: str, degree: int, high_coeff_mult: int, deadline: int,
             collengine: str, worker_token: str, coeffs: list[str]) -> int:
    """Create a fresh job: write meta and one available workunit per coefficient.

    `coeffs` is the explicit curated list (e.g. from coeff_list.txt). Idempotent on
    coefficients (duplicates are skipped). Returns the number of workunits added.
    """
    now = int(time.time())
    meta = {
        "schema_version": "1", "n": n, "degree": str(degree),
        "high_coeff_mult": str(high_coeff_mult), "deadline": str(deadline),
        "collengine": collengine, "worker_token": worker_token,
        "created_at": str(now), "coeff_source": "explicit-list",
    }
    conn.execute("BEGIN IMMEDIATE")
    conn.executemany("INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)", list(meta.items()))
    added = _insert_coeffs(conn, coeffs, now)
    conn.execute("COMMIT")
    return added


def extend_job(conn, coeffs: list[str]) -> int:
    """Append more coefficients as available workunits (no restart needed)."""
    now = int(time.time())
    conn.execute("BEGIN IMMEDIATE")
    added = _insert_coeffs(conn, coeffs, now)
    conn.execute("COMMIT")
    return added


def _insert_coeffs(conn, coeffs, now) -> int:
    added = 0
    for c in coeffs:
        c = c.strip()
        if not c:
            continue
        try:
            conn.execute(
                "INSERT INTO workunits(id,coeff,state,created_at,updated_at) "
                "VALUES(?,?,'available',?,?)",
                (f"wu-{c}", c, now, now),
            )
            added += 1
        except sqlite3.IntegrityError:
            pass  # coefficient already present — keep extend idempotent
    return added


def get_meta(conn) -> dict:
    return {r["key"]: r["value"] for r in conn.execute("SELECT key,value FROM meta")}


def stats(conn) -> dict:
    by_state = {r["state"]: r["n"]
                for r in conn.execute("SELECT state, COUNT(*) n FROM workunits GROUP BY state")}
    agg = conn.execute(
        "SELECT COUNT(*) subs, COALESCE(SUM(poly_count),0) polys "
        "FROM submissions WHERE verify_status IN ('passed','skipped')"
    ).fetchone()
    return {"workunits": by_state, "submissions": agg["subs"], "polys": agg["polys"]}


# --------------------------------------------------------------------------- #
# Worker API state machine (Phase 1 — TODO)
# --------------------------------------------------------------------------- #

def lease(conn, client_id: str, lease_seconds: int):
    """Atomically claim one available (or lease-expired) workunit.

    TODO Phase 1 — one statement, lowest coefficient first:
        UPDATE workunits
           SET state='leased', client_id=:cid, lease_expires=:now+:lease, updated_at=:now
         WHERE id = (SELECT id FROM workunits
                      WHERE state='available'
                         OR (state='leased' AND lease_expires < :now)
                      ORDER BY CAST(coeff AS INTEGER) LIMIT 1)
        RETURNING *;
    Return the row, or None if nothing is available.
    """
    raise NotImplementedError("Phase 1: db.lease")


def submit(conn, *, workunit_id, client_id, sha256, bytes_, poly_count, path) -> bool:
    """Record a submission (verify_status='pending') and move the workunit
    leased→submitted, only if it is currently leased to this client.

    TODO Phase 1: BEGIN IMMEDIATE; return False (→ HTTP 409) if not leased.
    """
    raise NotImplementedError("Phase 1: db.submit")


def release(conn, workunit_id, client_id) -> None:
    """Voluntary lease return on client cancel: leased→available, no attempt bump.

    TODO Phase 1.
    """
    raise NotImplementedError("Phase 1: db.release")


def sweep_expired(conn) -> int:
    """Requeue expired leases (→available, attempt_count++; →poisoned past max-attempts).

    TODO Phase 1: periodic timer in serve.
    """
    raise NotImplementedError("Phase 1: db.sweep_expired")

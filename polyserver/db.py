"""SQLite metadata store for poly-server.

Metadata only — raw .ms polynomial blobs live on disk under <jobdir>/polys/,
content-addressed and zstd-compressed (DESIGN.md §9). One job per server.

Concurrency: uvicorn runs sync handlers in a threadpool, so the single connection is
opened check_same_thread=False and *every* DB call in app.py is serialized by one lock
(atomicity of lease's SELECT-then-UPDATE relies on that lock). The server is therefore
single-process by design — do not run multiple uvicorn workers. WAL + busy_timeout cover
the rest. The Phase-2 verifier will open its own connection.
"""
from __future__ import annotations

import pathlib
import sqlite3
import time

SCHEMA_PATH = pathlib.Path(__file__).resolve().parent.parent / "schema.sql"

DEFAULT_MAX_ATTEMPTS = 5


def connect(db_path: str) -> sqlite3.Connection:
    # check_same_thread=False: shared across uvicorn's threadpool; app.py serializes with a lock.
    conn = sqlite3.connect(db_path, isolation_level=None, check_same_thread=False)
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
    Caller (cmd_init) validates that coefficients are positive decimals and guards reuse.
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
# Worker API state machine (DESIGN.md §5)
# --------------------------------------------------------------------------- #

def lease(conn, client_id: str, lease_seconds: int):
    """Claim the lowest-coefficient available workunit; mark it leased.

    SELECT-then-UPDATE is atomic *because the caller holds the app lock* (see module
    docstring). Ordering is bignum-safe (`length, value`) since coeff is TEXT and may
    exceed 64-bit; never CAST it. Lease-expired units are returned to 'available' by
    sweep_expired(), not re-leased here. Returns the row or None.
    """
    now = int(time.time())
    row = conn.execute(
        "SELECT id, coeff, attempt_count FROM workunits "
        "WHERE state='available' ORDER BY length(coeff), coeff LIMIT 1"
    ).fetchone()
    if row is None:
        return None
    conn.execute(
        "UPDATE workunits SET state='leased', client_id=?, lease_expires=?, updated_at=? WHERE id=?",
        (client_id, now + lease_seconds, now, row["id"]),
    )
    return row


def submit(conn, *, workunit_id, client_id, sha256, comp_sha256, bytes_, poly_count,
           path, verify_status="passed") -> str:
    """Record a successful submission and move the workunit leased→submitted.

    Returns one of:
      'accepted'  — recorded; workunit → submitted.
      'duplicate' — same workunit+sha already recorded (a lost-ACK retry); treat as success.
      'conflict'  — not leased to this client (→ HTTP 409).
      'unknown'   — no such workunit (→ HTTP 404).

    Phase 1 'submitted' is terminal (no verifier advances it to 'verified'); Phase 2's
    background mod-N verifier will. verify_status is 'passed' (cheap c_d check ok) or
    'skipped' (checks disabled).
    """
    now = int(time.time())
    conn.execute("BEGIN IMMEDIATE")
    wu = conn.execute("SELECT state, client_id FROM workunits WHERE id=?",
                      (workunit_id,)).fetchone()
    if wu is None:
        conn.execute("ROLLBACK")
        return "unknown"
    dup = conn.execute(
        "SELECT 1 FROM submissions WHERE workunit_id=? AND sha256=? AND verify_status!='failed'",
        (workunit_id, sha256),
    ).fetchone()
    if dup is not None:
        conn.execute("ROLLBACK")
        return "duplicate"
    if wu["state"] != "leased" or (client_id and wu["client_id"] != client_id):
        conn.execute("ROLLBACK")
        return "conflict"
    conn.execute("UPDATE workunits SET state='submitted', updated_at=? WHERE id=?",
                 (now, workunit_id))
    conn.execute(
        "INSERT INTO submissions"
        "(workunit_id,sha256,comp_sha256,bytes,poly_count,verify_status,archived,path,client_id,submitted_at) "
        "VALUES(?,?,?,?,?,?,0,?,?,?)",
        (workunit_id, sha256, comp_sha256, bytes_, poly_count, verify_status, path, client_id, now),
    )
    conn.execute("COMMIT")
    return "accepted"


def fail_workunit(conn, *, workunit_id, client_id, max_attempts=DEFAULT_MAX_ATTEMPTS):
    """Verification (or processing) failed: requeue the workunit (attempt_count++) or
    poison it past the cap. Acts only if it is currently leased to this client.
    Returns 'available', 'poisoned', or None (no-op). No submission row is recorded —
    the failure is reflected in attempt_count / poisoned state.
    """
    now = int(time.time())
    conn.execute("BEGIN IMMEDIATE")
    wu = conn.execute("SELECT state, client_id, attempt_count FROM workunits WHERE id=?",
                      (workunit_id,)).fetchone()
    if wu is None or wu["state"] != "leased" or (client_id and wu["client_id"] != client_id):
        conn.execute("ROLLBACK")
        return None
    if wu["attempt_count"] + 1 >= max_attempts:
        conn.execute("UPDATE workunits SET state='poisoned', updated_at=? WHERE id=?",
                     (now, workunit_id))
        new = "poisoned"
    else:
        conn.execute(
            "UPDATE workunits SET state='available', attempt_count=attempt_count+1, "
            "client_id=NULL, lease_expires=NULL, updated_at=? WHERE id=?",
            (now, workunit_id))
        new = "available"
    conn.execute("COMMIT")
    return new


def release(conn, workunit_id, client_id) -> None:
    """Voluntary lease return on client cancel: leased→available, no attempt bump."""
    now = int(time.time())
    conn.execute(
        "UPDATE workunits SET state='available', client_id=NULL, lease_expires=NULL, updated_at=? "
        "WHERE id=? AND state='leased' AND client_id=?",
        (now, workunit_id, client_id),
    )


def sweep_expired(conn, max_attempts: int = DEFAULT_MAX_ATTEMPTS):
    """Requeue expired leases: →available (attempt_count++), or →poisoned past max_attempts.

    Returns (requeued, poisoned). Run on a periodic timer in serve.
    """
    now = int(time.time())
    conn.execute("BEGIN IMMEDIATE")
    rows = conn.execute(
        "SELECT id, attempt_count FROM workunits WHERE state='leased' AND lease_expires < ?",
        (now,),
    ).fetchall()
    requeued = poisoned = 0
    for r in rows:
        if r["attempt_count"] + 1 >= max_attempts:
            conn.execute("UPDATE workunits SET state='poisoned', updated_at=? WHERE id=?",
                         (now, r["id"]))
            poisoned += 1
        else:
            conn.execute(
                "UPDATE workunits SET state='available', attempt_count=attempt_count+1, "
                "client_id=NULL, lease_expires=NULL, updated_at=? WHERE id=?",
                (now, r["id"]),
            )
            requeued += 1
    conn.execute("COMMIT")
    return requeued, poisoned

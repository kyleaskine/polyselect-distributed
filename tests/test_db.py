"""Fast unit test of the poly-server DB state machine + verify parser. Stdlib only
(sqlite3) — no third-party deps, no GPU, no HTTP. Run: python3 tests/test_db.py
"""
import os
import pathlib
import shutil
import sys
import tempfile
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from polyserver import db, verify


def main():
    # verify.py parser (zstd import is lazy, so this needs no deps)
    rec = b"n: 123\nc5: 60060\nc4: 7\nc3: 5\nc2: 3\nc1: 2\nc0: 1\nY1: 9\nY0: -8"
    assert verify._leading_coeff(rec, b"c5:") == "60060"
    assert verify._leading_coeff(rec, b"c6:") is None
    assert verify._leading_coeff(b"c50: 9\nc5: 42", b"c5:") == "42"  # colon disambiguates
    print("verify parser OK")

    d = tempfile.mkdtemp(prefix="poly_dbtest_")
    try:
        conn = db.connect(os.path.join(d, "job.db"))
        db.init_schema(conn)
        assert db.init_job(conn, n="123456789", degree=5, high_coeff_mult=60060, deadline=100,
                           collengine="gerbicz", worker_token="t",
                           coeffs=["60060", "120120", "180180"]) == 3

        # bignum-safe ordering: 60060 (len 5) before 120120/180180 (len 6)
        l1 = db.lease(conn, "cA", 3600); assert l1["coeff"] == "60060", dict(l1)
        l2 = db.lease(conn, "cB", 3600); assert l2["coeff"] == "120120", dict(l2)
        assert db.stats(conn)["workunits"] == {"available": 1, "leased": 2}

        S = dict(workunit_id=l1["id"], client_id="cA", sha256="aa", comp_sha256="zz",
                 bytes_=10, poly_count=5, path="polys/a.zst")
        assert db.submit(conn, **S) == "accepted"
        assert db.submit(conn, **S) == "duplicate"                                  # lost-ACK retry
        assert db.submit(conn, workunit_id=l2["id"], client_id="cX", sha256="bb",
                         comp_sha256="yy", bytes_=1, poly_count=1, path="b") == "conflict"
        assert db.submit(conn, workunit_id="wu-nope", client_id="cZ", sha256="cc",
                         comp_sha256="xx", bytes_=1, poly_count=1, path="c") == "unknown"
        s = db.stats(conn)
        assert s["workunits"] == {"available": 1, "leased": 1, "submitted": 1}, s
        assert s["submissions"] == 1 and s["polys"] == 5, s

        # fail_workunit: requeue (attempt++) then poison at the cap
        db.release(conn, l2["id"], "cB")
        l3 = db.lease(conn, "cC", 3600); assert l3["coeff"] == "120120"
        assert db.fail_workunit(conn, workunit_id=l3["id"], client_id="cC", max_attempts=2) == "available"
        l4 = db.lease(conn, "cC", 3600); assert l4["coeff"] == "120120" and l4["attempt_count"] == 1, dict(l4)
        assert db.fail_workunit(conn, workunit_id=l4["id"], client_id="cC", max_attempts=2) == "poisoned"
        assert db.fail_workunit(conn, workunit_id=l4["id"], client_id="cC", max_attempts=2) is None
        assert db.stats(conn)["workunits"].get("poisoned") == 1

        # sweep still requeues an expired lease
        l5 = db.lease(conn, "cD", 3600); assert l5["coeff"] == "180180"
        conn.execute("UPDATE workunits SET lease_expires=? WHERE id=?", (int(time.time()) - 1, l5["id"]))
        assert db.sweep_expired(conn, max_attempts=5) == (1, 0)

        print("db state machine OK:", db.stats(conn))
        print("DB TEST: ALL PASSED")
    finally:
        shutil.rmtree(d, ignore_errors=True)


if __name__ == "__main__":
    main()

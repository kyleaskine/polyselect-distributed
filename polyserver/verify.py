"""Lightweight structural verification of an uploaded .ms (DESIGN.md §10).

Phase 1 (cheap, on the request path): decompress only a small leading prefix, parse up to
K polynomial records, and confirm each has leading coefficient c<degree> == the assigned
coefficient. This catches the realistic failure mode — a rented box with a mismatched
msieve build emitting the wrong coefficient or garbage — before it costs GPU-hours or
pollutes the optimizer, at negligible cost (a record is a few hundred bytes; K=50 reads
~tens of KB decompressed and stops).

Phase 2 will add random reservoir sampling across the whole file plus the algebraic
mod-N homomorphism check (Σ c_i·(−Y0)^i·Y1^(d−i) ≡ 0 mod N) in a background queue.
"""
from __future__ import annotations

# Safety bound: stop decompressing after this much even if we never find K records.
_MAX_SCAN = 4 * 1024 * 1024


def sample_check(blob_path, *, degree: int, coeff: str, k: int = 50):
    """Return (ok, checked, reason).

    ok=True iff the first up-to-K records all parse and have c<degree> == coeff. A
    genuinely empty result (0 decompressed bytes) is accepted — a coefficient can legally
    yield no polynomials. Non-empty output that yields no parseable record is rejected.
    """
    import zstandard  # lazy: lets the parser (_leading_coeff) be imported/tested without the dep

    want = f"c{degree}:".encode()
    coeff = str(coeff).strip()
    dctx = zstandard.ZstdDecompressor()
    buf = b""
    scanned = 0
    records: list[bytes] = []
    with open(blob_path, "rb") as f, dctx.stream_reader(f) as r:
        while len(records) < k and scanned < _MAX_SCAN:
            chunk = r.read(1 << 16)
            if not chunk:
                break
            scanned += len(chunk)
            buf += chunk
            while b"\n\n" in buf and len(records) < k:
                rec, buf = buf.split(b"\n\n", 1)
                if rec.strip():
                    records.append(rec)

    if scanned == 0:
        return True, 0, "empty result (no polynomials)"
    if not records:
        return False, 0, "non-empty upload but no parseable polynomial records"

    for rec in records:
        cd = _leading_coeff(rec, want)
        if cd is None:
            return False, len(records), f"record missing c{degree} leading-coefficient line"
        if cd != coeff:
            return False, len(records), f"c{degree}={cd} != assigned coeff {coeff}"
    return True, len(records), "ok"


def _leading_coeff(record: bytes, want: bytes):
    for line in record.split(b"\n"):
        s = line.strip()
        if s.startswith(want):
            return s[len(want):].strip().decode("ascii", "replace")
    return None

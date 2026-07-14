"""Thin client for the aliquot-tracker HTTP API — the work source and result sink.

Two endpoints are all the MVP needs (paths in aliquot-tracker/src/app/api/…):

    GET  /api/gnfs-candidates?needsPolynomial=true   composites needing a poly (public)
    POST /api/sequences/{id}/polynomial              store a poly (X-Internal-Key = admin)

The internal key is *trusted as admin with no user lookup* (aliquot-tracker
src/lib/auth-helpers.ts:135), so the conductor just holds it. The MVP works one composite at
a time and uses no poly-reserving on the tracker (it has none yet) — dedup is simply "run one
at a time" (see the plan's deferred phases).
"""
from __future__ import annotations

import httpx


class Tracker:
    def __init__(self, base_url: str, internal_key: str | None = None,
                 api_key: str | None = None, *, timeout: float = 30):
        self.base = base_url.rstrip("/")
        self.internal_key = internal_key
        self.api_key = api_key
        self.timeout = timeout

    def _auth_headers(self) -> dict[str, str]:
        """Credentials the submit endpoint (requireAdminOrInternal) accepts: a personal
        X-Api-Key (owner must be an admin) and/or the shared server X-Internal-Key. Send
        whichever is set — if both, the tracker tries the internal key first, then the API key,
        so a stale internal key simply falls through to the API key."""
        h: dict[str, str] = {}
        if self.api_key:
            h["X-Api-Key"] = self.api_key
        if self.internal_key:
            h["X-Internal-Key"] = self.internal_key
        return h

    def candidates(self, *, min_digits: int | None = None,
                   needs_polynomial: bool = True) -> list[dict]:
        """GET the polyselect worklist feed. Returns the `data.candidates` list."""
        params: dict[str, str] = {}
        if needs_polynomial:
            params["needsPolynomial"] = "true"
        if min_digits is not None:
            params["minDigits"] = str(min_digits)
        r = httpx.get(f"{self.base}/api/gnfs-candidates", params=params, timeout=self.timeout)
        r.raise_for_status()
        return r.json().get("data", {}).get("candidates", [])

    def existing_poly(self, sequence_id: str) -> dict | None:
        """GET the currently stored polynomial for a sequence, or None if there is none."""
        r = httpx.get(f"{self.base}/api/sequences/{sequence_id}/polynomial", timeout=self.timeout)
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return r.json().get("data", {}).get("polynomial")

    def submit_poly(self, sequence_id: str, poly_text: str,
                    murphy_e: float | None = None) -> httpx.Response:
        """POST a polynomial. `murphy_e` is passed explicitly (it overrides the tracker's own
        scrape, which misses msieve's `e 3.8e-09` no-equals format). Returns the raw response
        so the caller can branch on 200/400(stale)/40x without an exception."""
        headers = self._auth_headers()
        if not headers:
            raise RuntimeError("no credential configured; set api_key (your admin API key) "
                               "or internal_key (the server's INTERNAL_API_KEY)")
        payload: dict = {"polyText": poly_text}
        if murphy_e is not None:
            payload["murphyE"] = murphy_e
        return httpx.post(
            f"{self.base}/api/sequences/{sequence_id}/polynomial",
            json=payload,
            headers=headers,
            timeout=max(self.timeout, 60),
        )


def select_candidate(candidates: list[dict], *, sequence_id: str | None = None,
                     start_number: str | None = None) -> dict | None:
    """Pick one candidate by sequenceId, by startNumber (the 'AS' seed), or the first
    eligible one when neither is given. Returns None when nothing matches."""
    if sequence_id is not None:
        return next((c for c in candidates if str(c.get("sequenceId")) == str(sequence_id)), None)
    if start_number is not None:
        return next((c for c in candidates if str(c.get("startNumber")) == str(start_number)), None)
    return candidates[0] if candidates else None

"""poly-server FastAPI app — worker API + read-only dashboard (DESIGN.md §6).

No admin endpoints: init/extend/prune are CLI subcommands run on the droplet over
SSH (DESIGN.md §8). Auth is a single low-privilege worker token.

Single-process by design: one shared SQLite connection guarded by an in-process lock.
Do NOT run multiple uvicorn workers (see polyserver/db.py).
"""
from __future__ import annotations

import hashlib
import hmac
import os
import pathlib
import threading
import time

import zstandard
from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

from . import db, verify

DASHBOARD_HTML = (pathlib.Path(__file__).resolve().parent / "dashboard.html").read_text()

# Decoded-size cap for an uploaded .ms (a job's whole corpus is rarely larger; DESIGN.md §11).
OUTPUT_MAX_BYTES = 2 * 1024 * 1024 * 1024  # 2 GiB


class JobInfo(BaseModel):
    N: str
    degree: int
    high_coeff_mult: int
    deadline: int
    collengine: str
    colllib_hint: str | None = None


class LeaseRequest(BaseModel):
    client_id: str


class LeaseResponse(BaseModel):
    workunit_id: str
    coeff: str
    job: JobInfo
    lease_seconds: int


def create_app(conn, *, jobdir: str, lease_seconds: int = 3600, sweep_interval: int = 60,
               max_attempts: int = db.DEFAULT_MAX_ATTEMPTS, spotcheck_k: int = 50) -> FastAPI:
    app = FastAPI(title="poly-server", version="0.0.1")
    meta = db.get_meta(conn)
    worker_token = meta["worker_token"]
    degree = int(meta["degree"])
    polys_dir = pathlib.Path(jobdir) / "polys"
    polys_dir.mkdir(exist_ok=True)
    lock = threading.Lock()  # serialize all access to the shared sqlite connection

    def require_worker(authorization: str = Header(default="")) -> None:
        token = authorization.removeprefix("Bearer ").strip()
        if not hmac.compare_digest(token, worker_token):
            raise HTTPException(status_code=401, detail="bad or missing worker token")

    def job_info() -> JobInfo:
        return JobInfo(
            N=meta["n"], degree=degree, high_coeff_mult=int(meta["high_coeff_mult"]),
            deadline=int(meta["deadline"]), collengine=meta["collengine"],
        )

    # Background lease-expiry sweep (daemon; dies with the process, like ggnfs today).
    def sweep_loop():
        while True:
            time.sleep(sweep_interval)
            try:
                with lock:
                    requeued, poisoned = db.sweep_expired(conn, max_attempts)
                if requeued or poisoned:
                    print(f"[sweep] requeued={requeued} poisoned={poisoned}", flush=True)
            except Exception as e:  # never let the daemon die
                print(f"[sweep] error: {e}", flush=True)

    threading.Thread(target=sweep_loop, daemon=True, name="sweep").start()

    @app.get("/health")
    def health():
        return {"ok": True, "ts": int(time.time())}

    @app.post("/lease")
    def lease(req: LeaseRequest, _=Depends(require_worker)):
        with lock:
            row = db.lease(conn, req.client_id, lease_seconds)
        if row is None:
            return Response(status_code=204)  # no work; client backs off and retries
        return LeaseResponse(
            workunit_id=row["id"], coeff=row["coeff"],
            job=job_info(), lease_seconds=lease_seconds,
        ).model_dump()

    @app.post("/submit")
    async def submit(
        request: Request,
        x_workunit_id: str = Header(...),
        x_sha256: str = Header(...),
        x_poly_count: int = Header(default=0),
        x_client_id: str = Header(default=""),
        x_compression: str = Header(default="zstd"),
        _=Depends(require_worker),
    ):
        if x_compression != "zstd":
            raise HTTPException(status_code=415, detail="only zstd uploads are supported")
        with lock:
            row = conn.execute("SELECT coeff FROM workunits WHERE id=?",
                               (x_workunit_id,)).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="unknown workunit")
        coeff = row["coeff"]

        # Stream the (compressed) body to a temp file — no lock held during the transfer.
        # Hash the compressed bytes here (for the prune manifest) and the decompressed
        # stream below (content address + integrity).
        tmp = polys_dir / f".incoming-{x_workunit_id}-{os.getpid()}.tmp"
        comp_size = 0
        comp_h = hashlib.sha256()
        try:
            with open(tmp, "wb") as f:
                async for chunk in request.stream():
                    comp_size += len(chunk)
                    comp_h.update(chunk)
                    f.write(chunk)

            h = hashlib.sha256()
            raw_size = 0
            dctx = zstandard.ZstdDecompressor()
            with open(tmp, "rb") as f, dctx.stream_reader(f) as r:
                while True:
                    chunk = r.read(1 << 20)
                    if not chunk:
                        break
                    raw_size += len(chunk)
                    if raw_size > OUTPUT_MAX_BYTES:
                        raise HTTPException(status_code=413, detail="decoded .ms exceeds cap")
                    h.update(chunk)
            digest = h.hexdigest()
            if digest != x_sha256:
                raise HTTPException(status_code=400,
                                    detail=f"sha256 mismatch (client {x_sha256}, server {digest})")

            final = polys_dir / f"{coeff}-{digest}.ms.zst"
            tmp.replace(final)  # atomic move into the content-addressed path
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise

        # Cheap structural verification on a small sample (DESIGN.md §10).
        if spotcheck_k > 0:
            ok, _checked, reason = verify.sample_check(final, degree=degree, coeff=coeff,
                                                       k=spotcheck_k)
            if not ok:
                final.unlink(missing_ok=True)
                with lock:
                    db.fail_workunit(conn, workunit_id=x_workunit_id, client_id=x_client_id,
                                     max_attempts=max_attempts)
                raise HTTPException(status_code=422, detail=f"verification failed: {reason}")
            vstatus = "passed"
        else:
            vstatus = "skipped"

        with lock:
            status = db.submit(conn, workunit_id=x_workunit_id, client_id=x_client_id,
                               sha256=digest, comp_sha256=comp_h.hexdigest(), bytes_=comp_size,
                               poly_count=x_poly_count, path=f"polys/{final.name}",
                               verify_status=vstatus)
        if status in ("accepted", "duplicate"):
            return {"ok": True, "stored_sha": digest, "bytes": comp_size,
                    "duplicate": status == "duplicate"}
        if status == "unknown":
            raise HTTPException(status_code=404, detail="unknown workunit")
        raise HTTPException(status_code=409, detail="workunit not leased to this client")

    @app.post("/release")
    def release(
        x_workunit_id: str = Header(...),
        x_client_id: str = Header(...),
        _=Depends(require_worker),
    ):
        with lock:
            db.release(conn, x_workunit_id, x_client_id)
        return {"ok": True}

    @app.get("/stats")
    def stats(_=Depends(require_worker)):
        with lock:
            return JSONResponse(db.stats(conn))

    @app.get("/", response_class=HTMLResponse)
    def dashboard():
        return DASHBOARD_HTML

    return app

"""poly-server FastAPI app — worker API + read-only dashboard (DESIGN.md §6).

No admin endpoints: init/extend/prune are CLI subcommands run on the droplet over
SSH (DESIGN.md §8). Auth is a single low-privilege worker token.
"""
from __future__ import annotations

import hmac
import pathlib
import time

from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

from . import db

DASHBOARD_HTML = (pathlib.Path(__file__).resolve().parent / "dashboard.html").read_text()

# Keep in sync with the client's upload cap. zstd-decoded streams above this are rejected.
OUTPUT_MAX_BYTES = 2 * 1024 * 1024 * 1024  # 2 GiB (a job's corpus is rarely larger)


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


def create_app(conn, *, jobdir: str, lease_seconds: int = 3600) -> FastAPI:
    app = FastAPI(title="poly-server", version="0.0.1")
    meta = db.get_meta(conn)
    worker_token = meta["worker_token"]
    polys_dir = pathlib.Path(jobdir) / "polys"
    polys_dir.mkdir(exist_ok=True)

    def require_worker(authorization: str = Header(default="")) -> None:
        token = authorization.removeprefix("Bearer ").strip()
        if not hmac.compare_digest(token, worker_token):
            raise HTTPException(status_code=401, detail="bad or missing worker token")

    def job_info() -> JobInfo:
        return JobInfo(
            N=meta["n"], degree=int(meta["degree"]),
            high_coeff_mult=int(meta["high_coeff_mult"]),
            deadline=int(meta["deadline"]), collengine=meta["collengine"],
        )

    @app.get("/health")
    def health():
        return {"ok": True, "ts": int(time.time())}

    @app.post("/lease")
    def lease(req: LeaseRequest, _=Depends(require_worker)):
        row = db.lease(conn, req.client_id, lease_seconds)  # TODO Phase 1
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
        # TODO Phase 1:
        #   - stream the (zstd) request body to polys/<coeff>-<sha>.ms.zst
        #   - verify sha256 of the *decompressed* stream == x_sha256
        #   - reject decoded size > OUTPUT_MAX_BYTES
        #   - db.submit(...) (→ 409 if the workunit isn't leased to this client)
        #   - the Phase-2 background verifier flips verify_status pending→passed/failed
        raise HTTPException(status_code=501, detail="submit: Phase 1 TODO")

    @app.post("/release")
    def release(
        x_workunit_id: str = Header(...),
        x_client_id: str = Header(...),
        _=Depends(require_worker),
    ):
        db.release(conn, x_workunit_id, x_client_id)  # TODO Phase 1
        return {"ok": True}

    @app.get("/stats")
    def stats(_=Depends(require_worker)):
        return JSONResponse(db.stats(conn))

    @app.get("/", response_class=HTMLResponse)
    def dashboard():
        return DASHBOARD_HTML

    return app

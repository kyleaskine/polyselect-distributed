"""poly-client worker loop: lease a coefficient -> run msieve -> upload polys.

One msieve process per GPU (stage 1 saturates the GPU). Drain/cancel on Ctrl-C
mirrors ggnfs: first signal = finish the current unit + stop leasing; second = cancel,
kill the msieve child, release the lease (DESIGN.md §4). Drain/cancel + retry-on-down
are Phase-1 TODOs below.
"""
from __future__ import annotations

import hashlib
import pathlib
import time

import httpx
import zstandard

from . import msieve_runner


class Client:
    def __init__(self, server_url, token, msieve_bin, *, gpu=0, client_id="worker",
                 colllib=None, workroot="work"):
        self.server = server_url.rstrip("/")
        self.headers = {"Authorization": f"Bearer {token}"}
        self.msieve_bin = msieve_bin
        self.gpu = gpu
        self.client_id = client_id
        self.colllib = colllib
        self.workroot = pathlib.Path(workroot)

    def lease(self):
        r = httpx.post(f"{self.server}/lease", json={"client_id": self.client_id},
                       headers=self.headers, timeout=30)
        if r.status_code == 204:
            return None  # no work available
        r.raise_for_status()
        return r.json()

    def run_unit(self, lease: dict):
        job, coeff, wu = lease["job"], lease["coeff"], lease["workunit_id"]
        workdir = self.workroot / wu
        msieve_runner.build_workdir(str(workdir), job["N"], coeff)
        ms_path = msieve_runner.run(
            self.msieve_bin, str(workdir), gpu=self.gpu,
            high_coeff_mult=job["high_coeff_mult"], collengine=job["collengine"],
            colllib=self.colllib,
        )
        self.upload(wu, coeff, ms_path)

    def upload(self, workunit_id: str, coeff: str, ms_path: pathlib.Path):
        raw = pathlib.Path(ms_path).read_bytes()
        sha = hashlib.sha256(raw).hexdigest()
        poly_count = raw.count(b"\nY0:")  # one 'Y0:' line per CADO record (verifier corrects)
        blob = zstandard.ZstdCompressor(level=10).compress(raw)
        headers = {
            **self.headers,
            "X-Workunit-Id": workunit_id, "X-Sha256": sha,
            "X-Poly-Count": str(poly_count), "X-Client-Id": self.client_id,
            "X-Compression": "zstd",
        }
        # TODO Phase 1: stream the blob; on server-down keep the file and retry /submit
        # (don't lease new work), like ggnfs.
        r = httpx.post(f"{self.server}/submit", content=blob, headers=headers, timeout=300)
        r.raise_for_status()

    def loop(self):
        """lease -> run -> upload, backing off when there's no work.

        TODO Phase 1: SIGINT handling (drain then cancel), retry-on-down in upload().
        """
        while True:
            lease = self.lease()
            if lease is None:
                time.sleep(30)
                continue
            self.run_unit(lease)

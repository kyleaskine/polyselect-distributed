"""poly-client worker loop: lease a coefficient -> run msieve -> upload polys.

One msieve process per GPU (stage 1 saturates the GPU). Drain/cancel on Ctrl-C mirrors
ggnfs: first signal = finish the current unit (including upload) and stop leasing;
second = cancel, kill the msieve child, release the lease, exit. Completed work is never
dropped — upload retries through a server outage, keeping the .ms.zst on disk. Uploads
stream from disk (so retries re-read, not re-buffer) and are idempotent server-side, so a
lost ACK is recovered rather than turned into a 409 (DESIGN.md §4).
"""
from __future__ import annotations

import hashlib
import pathlib
import shutil
import signal
import threading
import time

import httpx
import zstandard

from . import msieve_runner

_CHUNK = 1 << 20


def _file_chunks(path, size=_CHUNK):
    with open(path, "rb") as f:
        while True:
            b = f.read(size)
            if not b:
                break
            yield b


class Client:
    def __init__(self, server_url, token, msieve_bin, *, gpu=0, client_id="worker",
                 workroot="work", idle_sleep=30, retry_sleep=15, max_failures=3):
        self.server = server_url.rstrip("/")
        self.headers = {"Authorization": f"Bearer {token}"}
        self.msieve_bin = msieve_bin
        self.gpu = gpu
        self.client_id = client_id
        self.workroot = pathlib.Path(workroot)
        self.idle_sleep = idle_sleep
        self.retry_sleep = retry_sleep
        self.max_failures = max_failures   # consecutive msieve failures before giving up
        self._drain = threading.Event()    # stop leasing new work; finish the current unit
        self._cancel = threading.Event()   # kill current msieve + release + exit
        self._held = None                  # workunit_id of the lease we currently hold

    # ---- signal handling (installed in loop(), which runs on the main thread) ----
    def _on_sigint(self, *_):
        if not self._drain.is_set():
            print("\n[drain] finishing current unit, no new work. Ctrl-C again to cancel.", flush=True)
            self._drain.set()
        else:
            print("\n[cancel] killing msieve and releasing the lease.", flush=True)
            self._cancel.set()

    def _on_sigterm(self, *_):
        # systemd stop / kill: clean immediate stop — kill msieve, release lease, exit.
        print("\n[term] SIGTERM — killing msieve, releasing lease, exiting.", flush=True)
        self._cancel.set()

    # ---- HTTP ----
    def _lease(self):
        r = httpx.post(f"{self.server}/lease", json={"client_id": self.client_id},
                       headers=self.headers, timeout=30)
        if r.status_code == 204:
            return None
        r.raise_for_status()
        return r.json()

    def _release(self, workunit_id):
        try:
            httpx.post(f"{self.server}/release", timeout=30,
                       headers={**self.headers, "X-Workunit-Id": workunit_id,
                                "X-Client-Id": self.client_id})
        except httpx.HTTPError:
            pass  # best-effort; the lease will expire and requeue anyway

    def _submit(self, workunit_id, zst_path, sha, poly_count):
        r = httpx.post(f"{self.server}/submit", content=_file_chunks(zst_path), timeout=600,
                       headers={**self.headers, "X-Workunit-Id": workunit_id,
                                "X-Sha256": sha, "X-Poly-Count": str(poly_count),
                                "X-Client-Id": self.client_id, "X-Compression": "zstd"})
        r.raise_for_status()

    # ---- one unit of work ----
    def _run_unit(self, lease):
        """Run one workunit. Returns True if the lease was resolved server-side (submitted,
        or a 4xx the server already handled) so the caller must NOT release it; returns None
        if it bailed still holding the lease (cancel mid-upload). Raises on msieve failure."""
        job, coeff, wu = lease["job"], lease["coeff"], lease["workunit_id"]
        workdir = self.workroot / wu
        msieve_runner.build_workdir(str(workdir), job["N"], coeff, self.msieve_bin)
        ms = msieve_runner.run(
            self.msieve_bin, str(workdir), gpu=self.gpu,
            high_coeff_mult=job["high_coeff_mult"],
            collengine=job.get("collengine", "gerbicz"),
            cancel=self._cancel.is_set,
        )

        # Stream the raw .ms once: sha256 + (approx) poly count + zstd -> a .zst on disk.
        zst = pathlib.Path(str(ms) + ".zst")
        sha = hashlib.sha256()
        poly_count = 0
        cctx = zstandard.ZstdCompressor(level=10)
        with open(ms, "rb") as fin, open(zst, "wb") as fout, \
                cctx.stream_writer(fout, closefd=False) as comp:
            while True:
                chunk = fin.read(_CHUNK)
                if not chunk:
                    break
                sha.update(chunk)
                poly_count += chunk.count(b"\nY0:")  # ~1 record per Y0 line (verifier corrects)
                comp.write(chunk)
        sha = sha.hexdigest()

        # Never drop completed work: retry through outages / 5xx; give up on 4xx (the
        # server has already handled it — accepted/duplicate=200, requeued=422, taken=409).
        while not self._cancel.is_set():
            try:
                self._submit(wu, zst, sha, poly_count)
                print(f"[done] {wu} coeff={coeff} polys~{poly_count} ({zst.stat().st_size} B zstd)", flush=True)
                shutil.rmtree(workdir, ignore_errors=True)
                return True   # resolved: leased→submitted; caller must not release
            except httpx.HTTPStatusError as e:
                code = e.response.status_code
                if 400 <= code < 500:
                    body = e.response.text[:200]
                    print(f"[submit] {wu} rejected {code}: {body} — server handled it; moving on", flush=True)
                    return True  # 4xx: server already resolved the lease (don't retry, don't release)
                print(f"[submit] {wu} server error {code}; retry in {self.retry_sleep}s", flush=True)
                time.sleep(self.retry_sleep)
            except httpx.HTTPError as e:
                print(f"[submit] {wu} unreachable ({e}); kept {zst}, retry in {self.retry_sleep}s", flush=True)
                time.sleep(self.retry_sleep)
        return None  # exited the retry loop on cancel — still holding the lease

    # ---- main loop ----
    def loop(self):
        signal.signal(signal.SIGINT, self._on_sigint)
        signal.signal(signal.SIGTERM, self._on_sigterm)
        self.workroot.mkdir(parents=True, exist_ok=True)
        print(f"[start] {self.client_id} gpu={self.gpu} -> {self.server}", flush=True)
        fails = 0
        try:
            while not self._drain.is_set() and not self._cancel.is_set():
                try:
                    lease = self._lease()
                except httpx.HTTPError as e:
                    print(f"[lease] server unreachable ({e}); retry in {self.retry_sleep}s", flush=True)
                    if self._wait(self.retry_sleep):
                        break
                    continue
                if lease is None:
                    print("[idle] no work available", flush=True)
                    if self._wait(self.idle_sleep):
                        break
                    continue

                wu = self._held = lease["workunit_id"]
                resolved = None
                try:
                    resolved = self._run_unit(lease)
                except msieve_runner.Cancelled:
                    pass  # cancel: released in the finally, then we break below
                except Exception as e:  # msieve crash / unexpected: don't die holding the lease
                    fails += 1
                    print(f"[error] {wu} failed ({e}); releasing lease [{fails} consecutive]", flush=True)
                finally:
                    if not resolved:        # exception or cancel — return the lease (server no-op if
                        self._release(wu)   # it's already resolved); on success _run_unit returned True
                    self._held = None

                if resolved:
                    fails = 0
                elif fails >= self.max_failures:
                    print(f"[fatal] {fails} consecutive failures — check the client/msieve setup; "
                          f"exiting.", flush=True)
                    break
                if self._cancel.is_set():
                    break
                if not resolved and not self._drain.is_set() and self._wait(self.retry_sleep):
                    break
        finally:
            if self._held:                  # last-ditch: never exit holding a lease
                self._release(self._held)
                self._held = None
        print("[stopped]", flush=True)

    def _wait(self, seconds):
        """Sleep in small slices; return True if a drain/cancel arrived (so callers exit)."""
        end = time.time() + seconds
        while time.time() < end:
            if self._drain.is_set() or self._cancel.is_set():
                return True
            time.sleep(0.5)
        return False

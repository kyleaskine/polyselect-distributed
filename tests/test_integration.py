"""End-to-end integration test: a real uvicorn poly-server in a thread + the real
poly-client over real HTTP, with a stub msieve. Exercises the wire layer the stdlib unit
tests can't reach: streaming zstd upload, server stream-decompress + sha verify + the real
verify.sample_check over a real zstd blob, lease/submit/verify/idempotency, and 204-no-work.

Run (needs the server+client deps installed — see requirements-{server,client}.txt):

    python3 tests/test_integration.py

No GPU and no real msieve required (a stub stands in). The real-msieve coeff_list=1
contract must still be validated separately on a GPU box (DESIGN.md §7).
"""
import hashlib
import os
import pathlib
import shutil
import socket
import sys
import tempfile
import threading
import time

HERE = pathlib.Path(__file__).resolve().parent
REPO = HERE.parent
STUB = str(HERE / "stub_msieve")
sys.path.insert(0, str(REPO))

try:
    import httpx
    import uvicorn
    import zstandard
except ImportError as e:
    sys.exit(f"missing dep ({e}); install: pip install -r requirements-server.txt "
             f"-r requirements-client.txt   (gmpy2 is not needed for this test)")

from polyserver import db
from polyserver.app import create_app
from polyclient import msieve_runner
from polyclient.client import Client

COEFFS = ["1260", "2520", "4620", "9240"]
N = ("167607202751713520755395238547505175579109747181766065729512289906719395455186020"
     "967457134128228376355120680855258479360585078682339636368676745445285657085644774"
     "094525261305605926676266686577843002833121")
TOKEN = "testtoken"

os.chmod(STUB, 0o755)
jobdir = pathlib.Path(tempfile.mkdtemp(prefix="poly_itest_"))
work = pathlib.Path(tempfile.mkdtemp(prefix="poly_iwork_"))

# Init in the main thread *before* serving (conn is touched only here pre-serve, then
# exclusively by the server threads under the app lock).
conn = db.connect(str(jobdir / "job.db"))
db.init_schema(conn)
db.init_job(conn, n=N, degree=5, high_coeff_mult=1260, deadline=100,
            collengine="gerbicz", worker_token=TOKEN, coeffs=COEFFS)
app = create_app(conn, jobdir=str(jobdir), lease_seconds=3600, spotcheck_k=50)

sock = socket.socket()
sock.bind(("127.0.0.1", 0))
PORT = sock.getsockname()[1]
sock.close()

server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=PORT, log_level="warning"))
server.install_signal_handlers = lambda: None  # uvicorn would otherwise signal() off the main thread
threading.Thread(target=server.run, daemon=True).start()

base = f"http://127.0.0.1:{PORT}"
H = {"Authorization": f"Bearer {TOKEN}"}


def stats():
    return httpx.get(base + "/stats", headers=H, timeout=10).json()


def n_blobs():
    return len(list((jobdir / "polys").glob("*.ms.zst")))


def main():
    for _ in range(100):
        try:
            if httpx.get(base + "/health", timeout=2).status_code == 200:
                break
        except Exception:
            pass
        time.sleep(0.1)
    else:
        sys.exit("FAIL: server did not start")

    client = Client(base, TOKEN, STUB, gpu=0, client_id="itest", workroot=str(work))

    # auth: missing token -> 401
    assert httpx.post(base + "/lease", json={"client_id": "x"}, timeout=10).status_code == 401
    print("auth 401 OK")

    # A. happy path E2E for 2 coeffs (lease -> stub msieve -> streaming upload -> verify+store)
    seen = set()
    for _ in range(2):
        lease = client._lease()
        assert lease is not None, "expected work"
        client._run_unit(lease)
        seen.add(lease["coeff"])
    assert seen == {"1260", "2520"}, seen
    st = stats()
    assert st["workunits"].get("submitted") == 2, st
    assert st["polys"] == 6 and n_blobs() == 2, (st, n_blobs())
    print("A happy-path E2E OK:", st)

    # B. verification reject (wrong c5) -> 422 -> requeued, blob deleted
    os.environ["STUB_BAD_C5"] = "1"
    lease = client._lease()
    assert lease["coeff"] == "4620", lease
    client._run_unit(lease)  # 422 -> client gives up (4xx), no raise
    del os.environ["STUB_BAD_C5"]
    st = stats()
    assert st["workunits"].get("submitted") == 2, st
    assert st["workunits"].get("available") == 2, st
    assert n_blobs() == 2, n_blobs()
    print("B verification-reject OK:", st)

    # C. reprocess the rejected coeff with a good result
    lease = client._lease()
    assert lease["coeff"] == "4620", lease
    client._run_unit(lease)
    st = stats()
    assert st["workunits"].get("submitted") == 3 and n_blobs() == 3, (st, n_blobs())
    print("C reprocess-after-reject OK:", st)

    # D. idempotency: re-submit same workunit+sha -> 200 duplicate (must not raise)
    lease = client._lease()
    wu, coeff = lease["workunit_id"], lease["coeff"]
    assert coeff == "9240", lease
    wd = msieve_runner.build_workdir(str(work / wu), N, coeff)
    ms = msieve_runner.run(STUB, str(wd), gpu=0, high_coeff_mult=1260)
    raw = pathlib.Path(ms).read_bytes()
    sha = hashlib.sha256(raw).hexdigest()
    zst = pathlib.Path(str(ms) + ".zst")
    zst.write_bytes(zstandard.ZstdCompressor(level=10).compress(raw))
    client._submit(wu, str(zst), sha, raw.count(b"\nY0:"))   # accepted
    client._submit(wu, str(zst), sha, raw.count(b"\nY0:"))   # duplicate -> 200
    st = stats()
    assert st["workunits"].get("submitted") == 4, st
    print("D idempotency OK:", st)

    # E. no work -> 204
    assert client._lease() is None
    print("E 204-no-work OK")

    st = stats()
    assert st["workunits"] == {"submitted": 4}, st
    assert st["polys"] == 12 and n_blobs() == 4, (st, n_blobs())
    print("FINAL:", st, "blobs:", n_blobs())
    print("INTEGRATION TEST: ALL PASSED")


try:
    main()
finally:
    server.should_exit = True
    shutil.rmtree(jobdir, ignore_errors=True)
    shutil.rmtree(work, ignore_errors=True)

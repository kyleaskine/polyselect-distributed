"""Tests for polylocal: the tracker client, best-poly extraction/validation, and the
end-to-end local pipeline (fetch -> stub msieve -> stub optimize -> validate -> submit) over
real HTTP against a stub tracker. Stdlib + httpx only; no GPU, no real msieve/CADO.

    python3 tests/test_polylocal.py

The real-msieve coeff_list=1 contract still needs validating on a GPU box (DESIGN.md §7).
"""
import json
import os
import pathlib
import shutil
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = pathlib.Path(__file__).resolve().parent
REPO = HERE.parent
STUB_MSIEVE = str(HERE / "stub_msieve")
sys.path.insert(0, str(REPO))

try:
    import httpx
except ImportError as e:
    sys.exit(f"missing dep ({e}); install: pip install -r requirements-client.txt")

from polylocal import optimize
from polylocal import __main__ as cli
from polylocal.tracker import Tracker, select_candidate
from polyclient import msieve_runner

# --- fixtures: a composite N and a polynomial that really shares a root mod N -------------
N = int("167607202751713520755395238547505175579109747181766065729512289906719395455186020"
        "967457134128228376355120680855258479360585078682339636368676745445285657085644774"
        "094525261305605926676266686577843002833121")
M = 12345                        # rational root: m = -Y0/Y1 = M
C5 = 720720
C0 = -((C5 * pow(M, 5)) % N)      # f(x) = C5 x^5 + C0  =>  f(M) = C5 M^5 + C0 ≡ 0 (mod N)
SEQ_ID = "seq-abc-123"
START_NUMBER = "552"
INTERNAL_KEY = "test-internal-key"
API_KEY = "test-admin-api-key"      # a personal key whose owner is an admin


def poly_block(escore, c0=C0):
    return (f"# norm 1.2e+28 alpha -6.5 e {escore} rroots 3\n"
            f"skew: 12345.0\nc5: {C5}\nc4: 0\nc3: 0\nc2: 0\nc1: 0\nc0: {c0}\nY1: 1\nY0: {-M}\n")


def cado_block(murphy):
    # CADO ropt layout: '### …' header, body, then a *trailing* '# side 1 MurphyE=' comment.
    return (f"### root-optimized polynomial ###\nn: {N}\nY0: {-M}\nY1: 1\n"
            f"c0: {C0}\nc1: 0\nc2: 0\nc3: 0\nc4: 0\nc5: {C5}\nskew: 12345.0\n"
            f"# side 1 MurphyE={murphy}\n")


# --- stub tracker ------------------------------------------------------------------------
class StubTracker(BaseHTTPRequestHandler):
    candidates: list = []
    submissions: list = []
    force_400: "str | None" = None   # set to an error string to force a post-auth 400 on POST

    def log_message(self, format, *args):   # match base signature; silence request logging
        pass

    def _send(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.startswith("/api/gnfs-candidates"):
            self._send(200, {"success": True, "data": {"candidates": StubTracker.candidates,
                       "count": len(StubTracker.candidates), "minDigits": 145}})
        elif "/polynomial" in self.path:
            self._send(404, {"success": False, "error": "No polynomial stored"})
        else:
            self._send(404, {"success": False})

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length) or b"{}")
        # requireAdminOrInternal: a valid shared internal key OR an admin's personal api key;
        # otherwise (no session either) 401 "Authentication required".
        if self.headers.get("X-Internal-Key") != INTERNAL_KEY \
                and self.headers.get("X-Api-Key") != API_KEY:
            return self._send(401, {"success": False, "error": "Authentication required"})
        if StubTracker.force_400 is not None:                  # simulate a post-auth rejection
            return self._send(400, {"success": False, "error": StubTracker.force_400})
        if f"n: {N}" not in body.get("polyText", ""):          # emulate the tracker's n-match
            return self._send(400, {"success": False, "error": "n does not match"})
        StubTracker.submissions.append(body)
        self._send(200, {"success": True, "message": "Polynomial stored",
                   "data": {"polynomial": {"murphyE": body.get("murphyE")}}})


def write_exec(path, text):
    pathlib.Path(path).write_text(text)
    os.chmod(path, 0o755)


# ---------------------------------------------------------------------------------------
def main():
    os.chmod(STUB_MSIEVE, 0o755)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), StubTracker)
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{port}"
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="polylocal_t_"))
    os.environ["POLYLOCAL_CONFIG"] = str(tmp / "isolate-none.ini")  # ignore any real polylocal.ini

    try:
        # 1. selection helper
        cands = [{"sequenceId": SEQ_ID, "startNumber": START_NUMBER, "index": 3,
                  "composite": str(N), "digits": 160},
                 {"sequenceId": "other", "startNumber": "660", "composite": "99", "digits": 2}]
        assert select_candidate(cands, start_number="552")["sequenceId"] == SEQ_ID
        assert select_candidate(cands, sequence_id="other")["startNumber"] == "660"
        assert select_candidate(cands)["sequenceId"] == SEQ_ID
        assert select_candidate([], start_number="1") is None
        print("1 select_candidate OK")

        # 2. extract_best: ranks by Murphy E, injects n:, drops any parsed n:
        text = "n: 999\n" + poly_block("1.00e-09") + "\n" + poly_block("3.14e-09")
        poly_text, me = optimize.extract_best(text, N)
        assert me == 3.14e-09, me
        assert poly_text.splitlines()[0] == f"n: {N}", poly_text.splitlines()[0]
        assert "n: 999" not in poly_text
        assert optimize.extract_best("# nothing here\n", N) is None
        print("2 extract_best OK (picked", me, "injected n:)")

        # 3. validate_poly: the constructed poly passes; a tampered c0 fails
        ok, why = optimize.validate_poly(poly_text, N)
        assert ok, why
        bad = poly_text.replace(f"c0: {C0}", f"c0: {C0 + 1}")
        ok2, why2 = optimize.validate_poly(bad, N)
        assert not ok2 and "root" in why2, (ok2, why2)
        ok3, _ = optimize.validate_poly(poly_text, N + 2)          # wrong composite
        assert not ok3
        print("3 validate_poly OK")

        # 3b. range-mode param table + argv (digit tiers, overrides, the num_polys stop)
        base_args = dict(min_coeff=None, high_coeff_mult=None, num_polys=None)
        assert cli._params_for(150, **base_args) == (420, 420, 400_000)
        assert cli._params_for(160, **base_args) == (2520, 2520, 800_000)
        assert cli._params_for(187, **base_args) == (138600, 138600, 1_600_000)
        assert cli._params_for(160, min_coeff=None, high_coeff_mult=None,
                               num_polys=2_000_000) == (2520, 2520, 2_000_000)   # per-flag override
        for d in (140, 200):                                   # too small / above table -> exit
            try:
                cli._params_for(d, **base_args)
                assert False, f"expected SystemExit for {d} digits"
            except SystemExit:
                pass
        assert cli._params_for(200, min_coeff=99, high_coeff_mult=99, num_polys=5) == (99, 99, 5)
        nps = msieve_runner.build_argv("ms", gpu=0, coeff_list=False, min_coeff=2520,
                                       high_coeff_mult=2520, num_polys=800_000)[-1]
        assert "coeff_list=1" not in nps and "min_coeff=2520" in nps and "num_polys=800000" in nps
        assert "high_coeff_mult=2520" in nps and "collengine=gerbicz" in nps
        assert "coeff_list=1" in msieve_runner.build_argv("ms", gpu=0)[-1]   # list mode unchanged
        print("3b range params + argv OK")

        # 3c. config file: _load_config + _resolve precedence (CLI > env > config > default)
        cfgfile = tmp / "polylocal.ini"
        cfgfile.write_text("[polylocal]\ntracker_url = http://from-config\ngpu = 2\n")
        cfg = cli._load_config(cfgfile)
        assert cfg["tracker_url"] == "http://from-config" and cfg["gpu"] == "2"
        assert cli._resolve("cli", "E_TEST", cfg, "tracker_url") == "cli"              # CLI wins
        os.environ["E_TEST"] = "http://from-env"
        assert cli._resolve(None, "E_TEST", cfg, "tracker_url") == "http://from-env"   # env > config
        del os.environ["E_TEST"]
        assert cli._resolve(None, "E_TEST", cfg, "tracker_url") == "http://from-config"  # config
        assert cli._resolve(None, None, {}, "x", "def") == "def"                       # default
        assert cli._load_config(tmp / "nope.ini") == {}                               # absent -> {}
        print("3c config precedence OK")

        # 3d. CADO ropt layout — '# side 1 MurphyE=' trails the body (the real file format);
        #     it must still rank correctly and parse the score, not orphan it.
        cado = cado_block("2.00e-09") + "\n" + cado_block("4.00e-09") + "\n" + cado_block("1.50e-09")
        pt, me = optimize.extract_best(cado, N)
        assert me == 4.00e-09, me
        assert pt.splitlines()[0] == f"n: {N}"
        ok, why = optimize.validate_poly(pt, N)
        assert ok, why
        print("3d CADO trailing-MurphyE OK")

        # 3e. code-review fixes: config-% (no interpolation), digit coercion, runnable check,
        #     explicit-0 override kept, single-parse count, and CRLF block splitting.
        pct = tmp / "pct.ini"
        pct.write_text("[polylocal]\napi_key = ab%cd\ntracker_url = http://x/%20y\n")
        assert cli._load_config(pct)["api_key"] == "ab%cd"                    # #1 no % crash
        assert cli._coerce_digits("166") == 166 and cli._coerce_digits(None) is None  # #5
        assert cli._coerce_digits("x") is None
        run = dict(poly_file=None, min_coeff=None, high_coeff_mult=None, num_polys=None)
        assert cli._is_runnable(160, **run) and not cli._is_runnable(200, **run)      # #3
        assert not cli._is_runnable(140, **run)
        assert cli._is_runnable(200, poly_file="x", min_coeff=None, high_coeff_mult=None, num_polys=None)
        assert cli._is_runnable(9999, poly_file=None, min_coeff=1, high_coeff_mult=1, num_polys=1)
        assert cli._params_for(160, min_coeff=None, high_coeff_mult=None,
                               num_polys=0) == (2520, 2520, 0)                # #6 explicit 0 kept
        assert cli._params_for(200, min_coeff=420, high_coeff_mult=420, num_polys=0) == (420, 420, 0)
        crlf = (cado_block("2.00e-09") + "\n" + cado_block("4.00e-09")).replace("\n", "\r\n")
        (_pt2, me2), nc = optimize.extract_best_and_count(crlf, N)            # #7 CRLF + #9 count
        assert me2 == 4.00e-09 and nc == 2, (me2, nc)
        print("3e review-fix units OK")

        # 4. submit_poly auth: shared internal key OR an admin's api key -> 200; bad poly 400;
        #    wrong/absent creds -> 401 (matches the tracker's requireAdminOrInternal path).
        StubTracker.candidates = cands
        StubTracker.submissions = []
        tr = Tracker(base, internal_key=INTERNAL_KEY)
        assert tr.candidates()[0]["sequenceId"] == SEQ_ID            # feed reachable + shaped
        assert tr.existing_poly(SEQ_ID) is None                      # 404 -> None
        assert tr.submit_poly(SEQ_ID, poly_text, me).status_code == 200                      # internal key
        assert Tracker(base, api_key=API_KEY).submit_poly(SEQ_ID, poly_text).status_code == 200  # api key
        assert tr.submit_poly(SEQ_ID, "skew: 1\nc2: 1\nc0: 0\nY1: 1\nY0: 0\n").status_code == 400  # bad poly
        assert Tracker(base, internal_key="wrong").submit_poly(SEQ_ID, poly_text).status_code == 401
        try:
            Tracker(base).submit_poly(SEQ_ID, poly_text)             # no credential configured
            assert False, "expected RuntimeError"
        except RuntimeError:
            pass
        assert len(StubTracker.submissions) == 2
        print("4 submit_poly auth (internal + api key) OK")

        # shared CLI fixtures
        stub_opt = tmp / "stub_optimize.sh"
        write_exec(stub_opt, "#!/usr/bin/env bash\ncat > outMsieve.p <<'POLY'\n"
                   + poly_block("1.00e-09") + "\n" + poly_block("3.14e-09") + "POLY\n")
        common = ["--tracker-url", base, "--internal-key", INTERNAL_KEY, "--msieve", STUB_MSIEVE]

        # 5. E2E: fetch -> stub msieve -> stub optimize -> validate -> submit
        StubTracker.candidates = cands
        StubTracker.submissions = []
        wd = tmp / "run5"
        rc = cli.main([*common, "--start-number", "552", "--workdir", str(wd),
                       "--optimize", "--nfs-optimize", str(stub_opt), "--submit"])
        assert rc == 0, rc
        assert (wd / SEQ_ID / "msieve.dat.ms").exists(), "corpus not produced"
        assert len(StubTracker.submissions) == 1, StubTracker.submissions
        sub = StubTracker.submissions[0]
        assert f"n: {N}" in sub["polyText"], "submitted poly missing n: line"
        assert abs(sub["murphyE"] - 3.14e-09) < 1e-20, sub["murphyE"]   # explicit, best-ranked
        print("5 E2E fetch->msieve->optimize->submit OK (murphyE", sub["murphyE"], ")")

        # 6. default run (no --optimize/--submit) stops at the corpus, submits nothing
        StubTracker.submissions = []
        wd = tmp / "run6"
        rc = cli.main([*common, "--next", "--workdir", str(wd)])
        assert rc == 0 and (wd / SEQ_ID / "msieve.dat.ms").exists()
        assert StubTracker.submissions == []
        print("6 default corpus-only run OK")

        # 7. --dry-run does not run msieve or submit
        StubTracker.submissions = []
        wd = tmp / "run7"
        rc = cli.main([*common, "--next", "--workdir", str(wd), "--dry-run"])
        assert rc == 0 and not (wd / SEQ_ID / "msieve.dat.ms").exists()
        assert StubTracker.submissions == []
        print("7 dry-run OK")

        # 8. --poly-file: a full nfs_optimize output (ranked blocks) -> picks the best,
        #    injects n:, submits; and a bare single poly (no '#' comment) still validates.
        StubTracker.submissions = []
        pf = tmp / "outMsieve.p"
        pf.write_text(poly_block("1.10e-09") + "\n" + poly_block("2.50e-09")
                      + "\n" + poly_block("1.90e-09"))
        rc = cli.main(["--tracker-url", base, "--api-key", API_KEY,   # CLI --api-key path
                       "--sequence-id", SEQ_ID, "--poly-file", str(pf), "--submit"])
        assert rc == 0, rc
        assert len(StubTracker.submissions) == 1
        assert f"n: {N}" in StubTracker.submissions[0]["polyText"]
        assert abs(StubTracker.submissions[0]["murphyE"] - 2.50e-09) < 1e-20   # best of three
        bare = tmp / "bare.p"
        bare.write_text(poly_block("2.50e-09").split("\n", 1)[1])              # drop the '#' line
        rc = cli.main(["--tracker-url", base, "--internal-key", INTERNAL_KEY,
                       "--sequence-id", SEQ_ID, "--poly-file", str(bare), "--dry-run"])
        assert rc == 0, rc                                                     # fallback validates
        print("8 --poly-file (ranked + bare) OK")

        # 9. no matching candidate -> clean SystemExit
        StubTracker.candidates = cands
        try:
            cli.main([*common, "--start-number", "999999", "--workdir", str(tmp / "run9")])
            assert False, "expected SystemExit"
        except SystemExit as e:
            assert "no matching runnable candidate" in str(e), e
        print("9 no-candidate error OK")

        # 10. genuine 400 (not an n-mismatch) -> hard error; stale 400 (n) -> benign return 0
        StubTracker.candidates = cands
        pf10 = tmp / "p10.p"
        pf10.write_text(poly_block("3.0e-09"))
        api = ["--tracker-url", base, "--api-key", API_KEY, "--sequence-id", SEQ_ID,
               "--poly-file", str(pf10), "--submit"]
        StubTracker.force_400 = "polynomial below Murphy-E threshold"
        try:
            cli.main(api)
            assert False, "expected SystemExit on a genuine 400"
        except SystemExit as e:
            assert "rejected 400" in str(e), e
        StubTracker.force_400 = "n does not match the current composite"
        assert cli.main(api) == 0                              # stale advance -> benign
        StubTracker.force_400 = None
        print("10 400 genuine-vs-stale OK")

        # 11. --next skips an out-of-table first candidate instead of aborting on it
        StubTracker.candidates = [
            {"sequenceId": "big", "startNumber": "999", "composite": str(N), "digits": 200},
            {"sequenceId": SEQ_ID, "startNumber": START_NUMBER, "composite": str(N), "digits": 160}]
        assert cli.main([*common, "--next", "--workdir", str(tmp / "run11"), "--dry-run"]) == 0
        print("11 --next skip-to-runnable OK")

        # 12. --submit on a corpus-only run (no --optimize/--poly-file) submits nothing
        StubTracker.candidates = cands
        StubTracker.submissions = []
        assert cli.main([*common, "--next", "--workdir", str(tmp / "run12"), "--submit"]) == 0
        assert StubTracker.submissions == []
        print("12 --submit corpus-only no-op OK")

        print("POLYLOCAL TEST: ALL PASSED")
    finally:
        httpd.shutdown()
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()

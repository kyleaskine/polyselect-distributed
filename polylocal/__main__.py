"""polylocal entrypoint — the local one-shot runner.

    # produce a corpus for a composite pulled from the tracker (runs msieve locally):
    python3 -m polylocal --start-number 552 --gpu 0 \
        --tracker-url http://aliquot.example.com:3002 --msieve /path/to/msieve

    # go all the way: optimize locally and submit the best polynomial back:
    python3 -m polylocal --next --optimize --submit \
        --nfs-optimize ~/msieve-s/nfs_optimize.sh --internal-key $INTERNAL_API_KEY …

    # submit a polynomial you optimized by hand (optimize stays manual for now):
    python3 -m polylocal --sequence-id <uuid> --poly-file best.p --submit …

Selector is one of --start-number / --sequence-id / --next. Optimize and submit are opt-in;
the default stops at a produced corpus and prints the next commands (like pull.sh).

msieve runs in range mode (no coeff_list to curate): min_coeff / high_coeff_mult / num_polys
default from N's digit count (see _params_for) and each is individually overridable — which is
also how you run a composite outside the built-in table. collengine=gerbicz is always used, and
degree is left to msieve (it derives it from N in -np1 mode).
"""
from __future__ import annotations

import argparse
import configparser
import os
import pathlib
import re
import sys

import httpx

from polyclient import msieve_runner

from . import optimize
from .tracker import Tracker, select_candidate

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent


def _load_config(path):
    """Read the [polylocal] section of an INI file into a dict; empty if the file is absent.
    interpolation=None so a literal '%' in a value (keys, URLs) is not treated as a token."""
    p = pathlib.Path(path).expanduser()
    if not p.is_file():
        return {}
    cfg = configparser.ConfigParser(interpolation=None)
    cfg.read(p)
    return dict(cfg["polylocal"]) if cfg.has_section("polylocal") else {}


def _coerce_digits(v):
    """The feed's digit count, coerced to int; None if missing or non-numeric."""
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _is_runnable(digits, *, poly_file, min_coeff, high_coeff_mult, num_polys):
    """Can a leased candidate actually be searched on this box? --poly-file needs no run; a
    full min_coeff/high_coeff_mult/num_polys override runs anything; otherwise the digit count
    must fall inside the built-in table."""
    if poly_file:
        return True
    if all(v is not None for v in (min_coeff, high_coeff_mult, num_polys)):
        return True
    return isinstance(digits, int) and _MIN_DIGITS <= digits < _PARAM_TABLE[-1][0]


def _resolve(cli_val, env_key, cfg, key, default=None):
    """Value precedence: CLI flag > environment variable > config file > built-in default."""
    if cli_val is not None:
        return cli_val
    if env_key and os.environ.get(env_key):
        return os.environ[env_key]
    if cfg.get(key):
        return cfg[key]
    return default


def _parse_args(argv):
    p = argparse.ArgumentParser(prog="polylocal")
    sel = p.add_mutually_exclusive_group(required=True)
    sel.add_argument("--start-number", dest="start_number",
                     help="the aliquot-sequence seed (e.g. 552 for AS552)")
    sel.add_argument("--sequence-id", dest="sequence_id", help="the tracker sequence UUID")
    sel.add_argument("--next", action="store_true", help="take the first eligible candidate")

    p.add_argument("--config", default=None,
                   help="path to a polylocal.ini (default: $POLYLOCAL_CONFIG or <repo>/polylocal.ini)")
    p.add_argument("--tracker-url", dest="tracker_url", default=None,
                   help="aliquot-tracker base URL (config tracker_url / $TRACKER_URL)")
    p.add_argument("--internal-key", dest="internal_key", default=None,
                   help="shared server X-Internal-Key for submit (config internal_key / $INTERNAL_API_KEY)")
    p.add_argument("--api-key", dest="api_key", default=None,
                   help="your personal X-Api-Key for submit; account must be admin "
                        "(config api_key / $TRACKER_API_KEY)")
    p.add_argument("--min-digits", dest="min_digits", type=int, default=None,
                   help="feed filter; the tracker defaults to 145")

    p.add_argument("--msieve", default=None,
                   help="path to the msieve binary (config msieve / $MSIEVE)")
    p.add_argument("--gpu", type=int, default=None, help="GPU index (config gpu; default 0)")
    p.add_argument("--workdir", default=None,
                   help="scratch root; each run uses <workdir>/<sequenceId> "
                        "(config workdir; default polylocal-work)")

    # Range-mode search params. Defaults come from N's digit count (see _params_for); passing
    # any one overrides its table value, and passing all three lets a composite outside the
    # table run too.
    p.add_argument("--min-coeff", dest="min_coeff", type=int, default=None,
                   help="lowest leading coefficient to search (default: from the digit table)")
    p.add_argument("--high-coeff-mult", dest="high_coeff_mult", type=int, default=None,
                   help="leading-coeff increment; msieve searches its smooth multiples "
                        "(default: = min_coeff, from the digit table)")
    p.add_argument("--num-polys", dest="num_polys", type=int, default=None,
                   help="stop after this many raw polynomials (default: from the digit table)")

    p.add_argument("--optimize", action="store_true",
                   help="run nfs_optimize.sh on the corpus and extract the best polynomial")
    p.add_argument("--nfs-optimize", dest="nfs_optimize", default=None,
                   help="path to nfs_optimize.sh (config nfs_optimize / $NFS_OPTIMIZE); "
                        "required with --optimize")
    p.add_argument("--optimize-args", dest="optimize_args", default="",
                   help="extra args appended to the nfs_optimize invocation")
    p.add_argument("--poly-file", dest="poly_file", default=None,
                   help="submit an already-optimized poly instead of running the pipeline; may "
                        "be a single .p or a full nfs_optimize output (the best is picked)")
    p.add_argument("--submit", action="store_true", help="POST the best polynomial to the tracker")
    p.add_argument("--dry-run", dest="dry_run", action="store_true",
                   help="fetch + build the msieve command (and validate --poly-file) but do "
                        "not run msieve or POST anything")
    return p.parse_args(argv)


def _resolve_msieve(path, *, required):
    if not path:
        if required:
            raise SystemExit("error: --msieve or $MSIEVE required")
        return None
    msieve = pathlib.Path(path).expanduser().resolve()
    if required and not msieve.is_file():
        raise SystemExit(f"error: --msieve binary not found: {msieve}")
    return msieve


# Range-mode search parameters keyed on N's digit count. Each tier is (upper_bound_exclusive,
# coeff, num_polys) with min_coeff == high_coeff_mult == coeff (a highly-composite value).
# Below _MIN_DIGITS is too small for this workflow; above the top tier needs explicit flags.
_MIN_DIGITS = 145
_PARAM_TABLE = [
    (159, 420,     400_000),
    (173, 2520,    800_000),
    (187, 27720,   1_200_000),
    (201, 138600,  1_600_000),
]


def _params_for(digits, *, min_coeff, high_coeff_mult, num_polys):
    """Resolve (min_coeff, high_coeff_mult, num_polys) from N's digit count, letting any
    explicitly-passed flag override its table value. A full override also runs a composite
    outside the table."""
    complete_override = all(v is not None for v in (min_coeff, high_coeff_mult, num_polys))
    if not complete_override:
        if digits is None:
            raise SystemExit("error: the tracker reported no digit count; pass --min-coeff, "
                             "--high-coeff-mult and --num-polys.")
        if digits < _MIN_DIGITS:
            raise SystemExit(f"error: N has {digits} digits (< C{_MIN_DIGITS}) — too small for "
                             f"this workflow; run msieve directly.")
        if digits >= _PARAM_TABLE[-1][0]:
            raise SystemExit(f"error: N has {digits} digits (>= C{_PARAM_TABLE[-1][0]}) — above "
                             f"the built-in table; pass --min-coeff, --high-coeff-mult and "
                             f"--num-polys to run it.")
    row = None
    for bound, coeff, polys in _PARAM_TABLE:
        if digits is not None and digits < bound:
            row = (coeff, coeff, polys)
            break
    if row is None:                      # complete override for a composite outside the table
        row = (min_coeff, high_coeff_mult, num_polys)
    mc, hcm, npoly = row
    # `is not None` (not `or`) so an explicit 0 — e.g. --num-polys 0 (msieve: no limit) — is kept.
    return (min_coeff if min_coeff is not None else mc,
            high_coeff_mult if high_coeff_mult is not None else hcm,
            num_polys if num_polys is not None else npoly)


def _size_for(digits):
    """Rough size preset for nfs_optimize from N's digit count (tunable)."""
    if not digits:
        return "medium"
    if digits < 150:
        return "small"
    if digits < 190:
        return "medium"
    return "big"


def _run_selection(msieve, n, params, workdir, *, gpu):
    # Local range mode: no coeff_list to curate. msieve enumerates smooth leading coeffs
    # a_d = k*high_coeff_mult from min_coeff upward and stops after num_polys polynomials
    # (our msieve num_polys= patch). collengine=gerbicz always (strictly better).
    min_coeff, high_coeff_mult, num_polys = params
    wd = msieve_runner.build_workdir(str(workdir), n, msieve_bin=str(msieve))  # no coeff_list.txt
    # forward_sigint: this is an interactive one-shot, so relay a terminal Ctrl-C into msieve's
    # own (separate) process group. msieve stops the search gracefully, keeping the polys it
    # already wrote, and the pipeline continues with that partial corpus.
    return msieve_runner.run(str(msieve), str(wd), gpu=gpu, collengine="gerbicz",
                             coeff_list=False, min_coeff=min_coeff,
                             high_coeff_mult=high_coeff_mult, num_polys=num_polys,
                             forward_sigint=True)


def _prepare_supplied(text, n):
    """Ready an operator-supplied .p for submission: ensure a `n: <n>` line and scrape E."""
    murphy = optimize.scrape_murphy_e(text)
    if not re.search(r"^\s*n\s*:", text, re.MULTILINE):
        text = f"n: {n}\n" + text
    return text, murphy


def main(argv=None):
    a = _parse_args(argv)
    cfg = _load_config(a.config or os.environ.get("POLYLOCAL_CONFIG") or REPO_ROOT / "polylocal.ini")

    tracker_url = _resolve(a.tracker_url, "TRACKER_URL", cfg, "tracker_url")
    if not tracker_url:
        raise SystemExit("error: set tracker_url in the config, --tracker-url, or $TRACKER_URL")
    tracker = Tracker(
        tracker_url,
        internal_key=_resolve(a.internal_key, "INTERNAL_API_KEY", cfg, "internal_key"),
        api_key=_resolve(a.api_key, "TRACKER_API_KEY", cfg, "api_key"),
    )
    gpu = int(_resolve(a.gpu, None, cfg, "gpu", 0))
    workdir_root = _resolve(a.workdir, None, cfg, "workdir", "polylocal-work")
    min_digits = a.min_digits if a.min_digits is not None else (
        int(cfg["min_digits"]) if cfg.get("min_digits") else None)

    # 1. Fetch + select (read-only).
    cands = tracker.candidates(min_digits=min_digits)
    if a.next:
        # first candidate actually runnable on this box — skip ones outside the digit table
        # (unless --poly-file or a full override makes them runnable) instead of aborting.
        cand = next((c for c in cands if _is_runnable(
            _coerce_digits(c.get("digits")), poly_file=a.poly_file, min_coeff=a.min_coeff,
            high_coeff_mult=a.high_coeff_mult, num_polys=a.num_polys)), None)
    else:
        cand = select_candidate(cands, sequence_id=a.sequence_id, start_number=a.start_number)
    if cand is None:
        avail = ", ".join(str(c.get("startNumber")) for c in cands) or "(none)"
        raise SystemExit(f"error: no matching runnable candidate needing a polynomial. "
                         f"Available AS seeds from the feed: {avail}")
    seq_id = str(cand["sequenceId"])
    n = str(cand["composite"])
    digits = _coerce_digits(cand.get("digits"))
    print(f"[pick] AS{cand.get('startNumber')} seq={seq_id} index={cand.get('index')} "
          f"digits={digits} N={n[:24]}…", flush=True)

    # 2. Obtain the polynomial: operator-supplied file, or run the local pipeline.
    murphy = None
    if a.poly_file:
        # Accept either a single polynomial or a full nfs_optimize output (many ranked blocks,
        # msieve `# … e … rroots` or CADO `# side 1 MurphyE=`): extract_best picks the highest
        # Murphy E and injects the correct n: line. Fall back to a bare poly with no `#` comment.
        raw = pathlib.Path(a.poly_file).read_text()
        best, ncand = optimize.extract_best_and_count(raw, n)
        poly_text, murphy = best if best is not None else _prepare_supplied(raw, n)
        print(f"[poly-file] {a.poly_file}: ranked {ncand} candidate(s)", flush=True)
    else:
        msieve = _resolve_msieve(_resolve(a.msieve, "MSIEVE", cfg, "msieve"), required=not a.dry_run)
        params = _params_for(digits, min_coeff=a.min_coeff,
                             high_coeff_mult=a.high_coeff_mult, num_polys=a.num_polys)
        min_coeff, high_coeff_mult, num_polys = params
        workdir = pathlib.Path(workdir_root) / seq_id
        preview = msieve_runner.build_argv(str(msieve or "msieve"), gpu=gpu, collengine="gerbicz",
                                          coeff_list=False, min_coeff=min_coeff,
                                          high_coeff_mult=high_coeff_mult, num_polys=num_polys)
        print(f"[select] min_coeff={min_coeff} high_coeff_mult={high_coeff_mult} "
              f"num_polys={num_polys} -> {workdir}\n         {' '.join(preview)}", flush=True)
        if a.dry_run:
            print("[dry-run] not running msieve; not submitting.")
            return 0
        corpus = _run_selection(msieve, n, params, workdir, gpu=gpu)
        print(f"[corpus] {corpus} ({corpus.stat().st_size} bytes)", flush=True)
        if not a.optimize:
            if a.submit:
                print("[warn] --submit needs a polynomial: add --optimize to optimize+submit "
                      "here, or submit later with --poly-file. Nothing submitted.", flush=True)
            print("[next] optimize the corpus, then submit the winner, e.g.:")
            print(f"       cd ~/msieve-s && ./nfs_optimize.sh ...            # against {corpus}")
            print(f"       python3 -m polylocal --sequence-id {seq_id} --poly-file <best.p> --submit …")
            return 0
        nfs = _resolve(a.nfs_optimize, "NFS_OPTIMIZE", cfg, "nfs_optimize")
        if not nfs:
            raise SystemExit("error: --optimize needs nfs_optimize in the config, "
                             "--nfs-optimize, or $NFS_OPTIMIZE")
        out = optimize.run_nfs_optimize(nfs, corpus, workdir=workdir / "opt",
                                        size=_size_for(digits), extra_args=a.optimize_args)
        best = optimize.extract_best(pathlib.Path(out).read_text(), n)
        if best is None:
            raise SystemExit(f"error: nfs_optimize ({out}) yielded no usable polynomial")
        poly_text, murphy = best

    # 3. Show the selected polynomial, validate locally (always), then submit if asked.
    print(f"[best] Murphy_E={murphy}\n{poly_text}", flush=True)
    ok, why = optimize.validate_poly(poly_text, n)
    if not ok:
        if a.dry_run:
            print(f"[dry-run] validation FAILED: {why}")
            return 1
        raise SystemExit(f"error: polynomial failed local validation: {why}")
    print("[valid] n matches and the sides share a root mod N", flush=True)

    if a.dry_run or not a.submit:
        print("[ok] validated, not submitting — drop --dry-run and add --submit to POST.")
        return 0
    r = tracker.submit_poly(seq_id, poly_text, murphy)
    if r.status_code in (200, 201):
        print(f"[submitted] tracker stored the polynomial for {seq_id} (Murphy_E={murphy})")
        return 0
    detail = r.text[:300]
    if r.status_code == 400 and "match" in detail.lower():
        # local validation already confirmed a shared root, so a 400 about n means the sequence
        # advanced between fetch and submit — the work is obsolete, not a genuine rejection.
        print(f"[stale] the sequence advanced past this composite; nothing stored ({detail})")
        return 0
    raise SystemExit(f"error: submit rejected {r.status_code}: {detail}")


if __name__ == "__main__":
    try:
        sys.exit(main())
    except httpx.HTTPError as e:
        raise SystemExit(f"error: tracker request failed ({e.__class__.__name__}): {e}")

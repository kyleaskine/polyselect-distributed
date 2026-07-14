"""Local size/root optimization + best-polynomial extraction and validation.

The coordinator droplet is too small to run this; it is a local-CPU step — the operator's
`~/msieve-s/nfs_optimize.sh` (CADO sopt + msieve -npr + CADO ropt). `extract_best` turns that
script's ranked output into one submittable polynomial, handling the two things the tracker's
validator (aliquot-tracker/src/utils/gnfsPoly.ts) needs but msieve's output lacks:

  1. an explicit `n:` line (msieve's best-poly body omits it), and
  2. a Murphy E we parse ourselves and pass explicitly — msieve prints `... e 3.8e-09
     rroots ...` (no `=`), which the tracker's `MurphyE=` scrape regex would miss.

`validate_poly` is a Python port of the tracker's `validatePolynomial`, used as a local
safety net before any real POST (and by `--dry-run`).
"""
from __future__ import annotations

import math
import pathlib
import re
import shlex
import shutil
import subprocess

# Murphy E as CADO "MurphyE=3.8e-09" / "Murphy_E 3.8e-09", or msieve "... e 3.8e-09 rroots".
_MURPHY_RES = (
    re.compile(r"murphy_?e\s*(?:\([^)]*\))?\s*=\s*([0-9][0-9.eE+-]*)", re.I),
    re.compile(r"murphy_?e\s+([0-9][0-9.eE+-]*)", re.I),
    re.compile(r"(?:^|\s)e\s+([0-9][0-9.eE+-]*)\s+rroots", re.I),
)
_KV_RE = re.compile(r"^([A-Za-z][A-Za-z0-9]*)\s*:\s*(.+?)\s*$")


def _parse_murphy_e(comment: str) -> float | None:
    for rx in _MURPHY_RES:
        m = rx.search(comment)
        if m:
            try:
                v = float(m.group(1))
            except ValueError:
                continue
            if math.isfinite(v) and v > 0:
                return v
    return None


def scrape_murphy_e(text: str) -> float | None:
    """Best Murphy E found on any `#` comment line of a poly file (for operator-supplied .p)."""
    best = None
    for line in text.splitlines():
        if line.lstrip().startswith("#"):
            me = _parse_murphy_e(line)
            if me is not None and (best is None or me > best):
                best = me
    return best


def _split_comment_leads(lines: list[str]) -> list[tuple[str, list[str]]]:
    """msieve-style split for a run with no blank separators: a `#` comment after a body
    begins the next polynomial (each poly is led by its `# norm … e … rroots` line)."""
    out: list[tuple[str, list[str]]] = []
    comments: list[str] = []
    body: list[str] = []
    for s in lines:
        if s.startswith("#"):
            if body:
                out.append((" ".join(comments), list(body)))
                comments, body = [], []
            comments.append(s)
        elif _KV_RE.match(s):
            body.append(s)
    if body:
        out.append((" ".join(comments), list(body)))
    return out


def _poly_blocks(text: str) -> list[tuple[str, list[str]]]:
    """Split optimizer output into (comment, body_lines) records, separated by blank lines.
    Within a record every `#` comment is gathered whether it *leads* the body (msieve:
    `# norm … e … rroots`) or *trails* it (CADO: `# side 1 MurphyE=…`). A record that holds
    several polynomials with no blank separator falls back to comment-leads (msieve) splitting."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")   # so CRLF blank lines still split
    blocks: list[tuple[str, list[str]]] = []
    for chunk in re.split(r"\n[ \t]*\n", text):
        lines = [ln.strip() for ln in chunk.splitlines() if ln.strip()]
        body_keys = [m.group(1).lower() for m in (_KV_RE.match(l) for l in lines) if m]
        if not body_keys:
            continue
        if len(body_keys) == len(set(body_keys)):        # a single polynomial in this record
            comments = " ".join(l for l in lines if l.startswith("#"))
            body = [l for l in lines if _KV_RE.match(l)]
            blocks.append((comments, body))
        else:                                            # several polys, no blank separator
            blocks.extend(_split_comment_leads(lines))
    return blocks


def _body_keys(body: list[str]) -> set[str]:
    keys = set()
    for b in body:
        m = _KV_RE.match(b)
        if m:
            keys.add(m.group(1).lower())
    return keys


def _complete(body: list[str]) -> bool:
    keys = _body_keys(body)
    has_c = any(k.startswith("c") and k[1:].isdigit() for k in keys)
    return has_c and "skew" in keys and "y0" in keys and "y1" in keys


def extract_best_and_count(text: str, n: int | str):
    """Rank the complete polynomials in `text` by Murphy E in a single parse. Returns
    (best, n_candidates) where best is (poly_text, murphy_e) ready to submit — a `n: <n>` line
    is prepended and any parsed `n:` dropped — or None if no complete polynomial is present.
    Blocks without a parseable score rank below scored ones (fallback for score-less output)."""
    best = None  # (sort_key, murphy_e, body)
    n_candidates = 0
    for comment, body in _poly_blocks(text):
        if not _complete(body):
            continue
        n_candidates += 1
        me = _parse_murphy_e(comment)
        key = me if me is not None else -math.inf
        if best is None or key > best[0]:
            best = (key, me, body)
    if best is None:
        return None, n_candidates
    _, me, body = best
    lines = [f"n: {n}"] + [b for b in body if not b.lower().startswith("n:")]
    return ("\n".join(lines) + "\n", me), n_candidates


def extract_best(text: str, n: int | str) -> tuple[str, float | None] | None:
    """Pick the highest-Murphy-E complete polynomial; see extract_best_and_count."""
    best, _ = extract_best_and_count(text, n)
    return best


def parse_poly(text: str) -> dict:
    """Parse a single CADO/ggnfs polynomial. Raises ValueError on a malformed/incomplete one."""
    n = y0 = y1 = None
    skew = None
    coeffs: dict[int, int] = {}
    for raw in text.splitlines():
        s = raw.strip()
        if not s or s.startswith("#"):
            continue
        m = _KV_RE.match(s)
        if not m:
            continue
        key, value = m.group(1).lower(), m.group(2)
        if key == "n":
            n = int(value)
        elif key == "skew":
            skew = float(value)
        elif key == "y0":
            y0 = int(value)
        elif key == "y1":
            y1 = int(value)
        elif re.fullmatch(r"c\d+", key):
            coeffs[int(key[1:])] = int(value)
    if n is None:
        raise ValueError("missing n")
    if skew is None:
        raise ValueError("missing skew")
    if skew <= 0:
        raise ValueError("skew must be positive")
    if y0 is None:
        raise ValueError("missing Y0")
    if y1 is None:
        raise ValueError("missing Y1")
    if y1 == 0:
        raise ValueError("Y1 must be nonzero")
    if not coeffs:
        raise ValueError("missing algebraic coefficients (c0..cd)")
    degree = max((p for p, c in coeffs.items() if c != 0), default=-1)
    return {"n": n, "skew": skew, "y0": y0, "y1": y1, "coeffs": coeffs, "degree": degree}


def validate_poly(text: str, composite: int | str) -> tuple[bool, str]:
    """Local port of the tracker's validatePolynomial: n must equal the composite, gcd(Y1,n)
    must be 1, and the sides must share a root mod n (f(-Y0/Y1) ≡ 0). Returns (ok, reason)."""
    try:
        p = parse_poly(text)
    except ValueError as e:
        return False, str(e)
    n = int(composite)
    if p["n"] != n:
        return False, "n does not match the composite"
    if p["degree"] < 2:
        return False, "algebraic polynomial must have degree at least 2"
    y1 = p["y1"]
    g = math.gcd(y1, n)
    if g != 1:
        return False, f"gcd(Y1, n) = {g} (submit as a factor, not a polynomial)"
    m = (-p["y0"] % n) * pow(y1, -1, n) % n
    acc = 0
    for i in range(p["degree"], -1, -1):
        acc = (acc * m + p["coeffs"].get(i, 0)) % n
    if acc % n != 0:
        return False, "algebraic and rational sides do not share a root mod n"
    return True, "ok"


def run_nfs_optimize(script: str, corpus: str | pathlib.Path, *, workdir: str | pathlib.Path,
                     size: str = "medium", extra_args: str = "",
                     timeout: float | None = None) -> pathlib.Path:
    """Best-effort local optimization: stage the corpus as `msieve.dat.ms` in `workdir` and
    run the operator's nfs_optimize.sh pipeline there; return the path to its `outMsieve.p`.

    The exact invocation depends on the operator's nfs_config.ini; the default is
    `nfs_optimize.sh pipeline --size <size>`. Override the tail via `--optimize-args`.
    """
    workdir = pathlib.Path(workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    staged = workdir / "msieve.dat.ms"
    if pathlib.Path(corpus).resolve() != staged.resolve():
        shutil.copyfile(corpus, staged)
    argv = ["bash", str(script), "pipeline", "--size", size, *shlex.split(extra_args)]
    subprocess.run(argv, cwd=str(workdir), check=True, timeout=timeout)
    out = workdir / "outMsieve.p"
    if not out.exists():
        raise RuntimeError(f"nfs_optimize produced no {out}")
    return out

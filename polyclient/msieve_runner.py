"""Build a per-workunit msieve workdir and run stage 1 for one coefficient.

The msieve invocation contract (DESIGN.md §7):

    workdir/worktodo.ini   = bare decimal N
    workdir/coeff_list.txt = the assigned coefficient (one per line)

    ./msieve -g <gpu> -np1 -nps "coeff_list=1 high_coeff_mult=M collengine=gerbicz colllib=<so>"

    -> emits workdir/msieve.dat.ms  (CADO-format raw polys, every c_d == coeff)

coeff_list=1 makes stage 1 read coefficients straight from coeff_list.txt and sieve
each directly (stage1.c:487-521), bypassing the find_next_ad range enumerator — so one
line = exactly one a_d. (min_coeff=max_coeff=C does NOT work; see DESIGN.md §7.)
"""
from __future__ import annotations

import pathlib
import subprocess


def build_workdir(workdir: str, n: str, coeff: str) -> pathlib.Path:
    d = pathlib.Path(workdir)
    d.mkdir(parents=True, exist_ok=True)
    (d / "worktodo.ini").write_text(n.strip() + "\n")
    (d / "coeff_list.txt").write_text(str(coeff).strip() + "\n")
    return d


def build_argv(msieve_bin: str, *, gpu: int, high_coeff_mult: int,
               collengine: str = "gerbicz", colllib: str | None = None) -> list[str]:
    args = f"coeff_list=1 high_coeff_mult={high_coeff_mult} collengine={collengine}"
    if colllib:
        args += f" colllib={colllib}"
    return [msieve_bin, "-g", str(gpu), "-np1", "-nps", args]


def run(msieve_bin: str, workdir: str, *, gpu: int, high_coeff_mult: int,
        collengine: str = "gerbicz", colllib: str | None = None, cancel=None) -> pathlib.Path:
    """Run msieve in `workdir`; return the path to msieve.dat.ms.

    Runs in its own process group (start_new_session) so the client's Ctrl-C can
    SIGTERM/SIGKILL the GPU child without racing the parent (mirrors ggnfs).

    TODO Phase 1:
      - poll `cancel()` while waiting and terminate the process group if set
      - surface msieve's stderr/log on failure
      - confirm a single-line coeff_list.txt yields exactly this one coefficient
    """
    argv = build_argv(msieve_bin, gpu=gpu, high_coeff_mult=high_coeff_mult,
                      collengine=collengine, colllib=colllib)
    proc = subprocess.Popen(argv, cwd=workdir, start_new_session=True)
    proc.wait()  # TODO: cancellation polling instead of a blocking wait
    out = pathlib.Path(workdir) / "msieve.dat.ms"
    if proc.returncode != 0 or not out.exists():
        raise RuntimeError(f"msieve failed (rc={proc.returncode}) or missing output {out}")
    return out

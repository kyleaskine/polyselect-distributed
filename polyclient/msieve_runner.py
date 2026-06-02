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

import os
import pathlib
import signal
import subprocess


class Cancelled(Exception):
    """Raised by run() when the cancel callback asks it to stop."""


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


def _terminate(proc: subprocess.Popen) -> None:
    """SIGTERM then SIGKILL the child's whole process group."""
    try:
        pgid = os.getpgid(proc.pid)
    except ProcessLookupError:
        return
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(pgid, sig)
        except ProcessLookupError:
            return
        try:
            proc.wait(timeout=5)
            return
        except subprocess.TimeoutExpired:
            continue


def run(msieve_bin: str, workdir: str, *, gpu: int, high_coeff_mult: int,
        collengine: str = "gerbicz", colllib: str | None = None,
        cancel=None, poll: float = 2.0) -> pathlib.Path:
    """Run msieve in `workdir`; return the path to msieve.dat.ms.

    Runs in its own process group (start_new_session=True) so the client can
    SIGTERM/SIGKILL the whole GPU job on cancel without racing the parent. `cancel` is a
    no-arg callable polled every `poll` seconds; truthy → kill the child and raise Cancelled.
    """
    argv = build_argv(msieve_bin, gpu=gpu, high_coeff_mult=high_coeff_mult,
                      collengine=collengine, colllib=colllib)
    proc = subprocess.Popen(argv, cwd=workdir, start_new_session=True)
    while True:
        try:
            proc.wait(timeout=poll)
            break
        except subprocess.TimeoutExpired:
            if cancel and cancel():
                _terminate(proc)
                raise Cancelled()

    out = pathlib.Path(workdir) / "msieve.dat.ms"
    if proc.returncode != 0 or not out.exists():
        raise RuntimeError(f"msieve failed (rc={proc.returncode}) or missing output {out}")
    return out

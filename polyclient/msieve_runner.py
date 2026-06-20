"""Build a per-workunit msieve workdir and run stage 1 for one coefficient.

The msieve invocation contract (DESIGN.md §7):

    workdir/worktodo.ini   = bare decimal N
    workdir/coeff_list.txt = the assigned coefficient (one per line)

    ./msieve -g <gpu> -np1 -nps "coeff_list=1 high_coeff_mult=M collengine=gerbicz"

    -> emits workdir/msieve.dat.ms  (CADO-format raw polys, every c_d == coeff)

coeff_list=1 makes stage 1 read coefficients straight from coeff_list.txt and sieve
each directly (stage1.c:487-521), bypassing the find_next_ad range enumerator — so one
line = exactly one a_d. (min_coeff=max_coeff=C does NOT work; see DESIGN.md §7.)

msieve loads several resources by path RELATIVE to its CWD: the GPU kernel
`stage1_core.ptx` (cuModuleLoad — no override flag) and the engine libs
`cub/collision_engine.so` + `cub/sort_engine.so` (relative defaults). Because we run each
workunit from an isolated workdir (not the msieve tree), build_workdir symlinks those into
the workdir so the relative loads resolve — which is also why no `colllib=`/`sortlib=` arg
is needed.
"""
from __future__ import annotations

import os
import pathlib
import signal
import subprocess


class Cancelled(Exception):
    """Raised by run() when the cancel callback asks it to stop."""


def build_workdir(workdir: str, n: str, coeff: str,
                  msieve_bin: str | None = None) -> pathlib.Path:
    d = pathlib.Path(workdir)
    d.mkdir(parents=True, exist_ok=True)
    (d / "worktodo.ini").write_text(n.strip() + "\n")
    (d / "coeff_list.txt").write_text(str(coeff).strip() + "\n")
    if msieve_bin:
        _link_support_files(d, pathlib.Path(msieve_bin).resolve().parent)
    return d


def _link_support_files(workdir: pathlib.Path, ms_dir: pathlib.Path) -> None:
    """Symlink msieve's CWD-relative runtime resources into the workdir (see module docstring):
    every *.ptx kernel in the msieve dir and the cub/ engine-lib dir. Skips any that don't
    exist (e.g. a stub msieve), so msieve's own load error still surfaces a real gap."""
    targets = list(ms_dir.glob("*.ptx"))
    cub = ms_dir / "cub"
    if cub.is_dir():
        targets.append(cub)
    for src in targets:
        link = workdir / src.name
        if link.is_symlink() or link.exists():
            link.unlink()
        link.symlink_to(src)


def build_argv(msieve_bin: str, *, gpu: int, high_coeff_mult: int = 0,
               collengine: str = "gerbicz") -> list[str]:
    # high_coeff_mult is inert in coeff_list mode (only the range enumerator reads it; see
    # DESIGN.md §7), so omit it when unset (0). Still emitted when nonzero for back-compat.
    # No colllib=/sortlib=: build_workdir symlinks cub/ so msieve's relative defaults resolve.
    args = "coeff_list=1"
    if high_coeff_mult:
        args += f" high_coeff_mult={high_coeff_mult}"
    args += f" collengine={collengine}"
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


def run(msieve_bin: str, workdir: str, *, gpu: int, high_coeff_mult: int = 0,
        collengine: str = "gerbicz", cancel=None, poll: float = 2.0) -> pathlib.Path:
    """Run msieve in `workdir`; return the path to msieve.dat.ms.

    Runs in its own process group (start_new_session=True) so the client can
    SIGTERM/SIGKILL the whole GPU job on cancel without racing the parent. `cancel` is a
    no-arg callable polled every `poll` seconds; truthy → kill the child and raise Cancelled.
    Assumes build_workdir already linked the support files into `workdir`.
    """
    argv = build_argv(msieve_bin, gpu=gpu, high_coeff_mult=high_coeff_mult,
                      collengine=collengine)
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

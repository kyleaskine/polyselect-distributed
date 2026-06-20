"""poly-client entrypoint.

    python3 -m polyclient --server-url http://host:8080 --token <worker-token> \
        --msieve /path/to/msieve --gpu 0

The collision/sort engine libs + GPU kernel are found relative to the msieve binary
(build_workdir symlinks them per workunit), so there is no --colllib to set.
"""
from __future__ import annotations

import argparse
import pathlib
import socket

from .client import Client


def main(argv=None):
    p = argparse.ArgumentParser(prog="poly-client")
    p.add_argument("--server-url", required=True)
    p.add_argument("--token", required=True, help="worker token")
    p.add_argument("--msieve", required=True, help="path to the msieve binary")
    p.add_argument("--gpu", type=int, default=0)
    p.add_argument("--client-id", default=None, help="defaults to the hostname")
    p.add_argument("--workroot", default="work")
    a = p.parse_args(argv)

    # Resolve ~ and relatives up front (the client chdir's into a workdir before launching
    # msieve, so a relative path would break there) and fail fast if it's missing.
    msieve = pathlib.Path(a.msieve).expanduser().resolve()
    if not msieve.is_file():
        raise SystemExit(f"error: --msieve binary not found: {msieve}")

    Client(
        a.server_url, a.token, str(msieve), gpu=a.gpu,
        client_id=a.client_id or socket.gethostname(),
        workroot=a.workroot,
    ).loop()


if __name__ == "__main__":
    main()

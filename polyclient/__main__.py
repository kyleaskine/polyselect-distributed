"""poly-client entrypoint.

    python3 -m polyclient --server-url http://host:8080 --token <worker-token> \
        --msieve /path/to/msieve --colllib /path/to/collision_engine.so --gpu 0
"""
from __future__ import annotations

import argparse
import socket

from .client import Client


def main(argv=None):
    p = argparse.ArgumentParser(prog="poly-client")
    p.add_argument("--server-url", required=True)
    p.add_argument("--token", required=True, help="worker token")
    p.add_argument("--msieve", required=True, help="path to the msieve binary")
    p.add_argument("--colllib", default=None, help="path to collision_engine .so")
    p.add_argument("--gpu", type=int, default=0)
    p.add_argument("--client-id", default=None, help="defaults to the hostname")
    p.add_argument("--workroot", default="work")
    a = p.parse_args(argv)

    Client(
        a.server_url, a.token, a.msieve, gpu=a.gpu,
        client_id=a.client_id or socket.gethostname(),
        colllib=a.colllib, workroot=a.workroot,
    ).loop()


if __name__ == "__main__":
    main()

#!/usr/bin/env bash
# pull.sh — pull the collected polynomial corpus down from the droplet over SSH,
# then optionally prune the server copy (DESIGN.md §11). Run on the workstation.
#
#   ./pull.sh --host droplet --jobdir /srv/polyjob --dest ~/corpus [--prune]
#
# Bulk transfer is rsync over SSH (your key) — incremental, resumable, encrypted.
# This replaces the manual "copy out of polys/ -> sftp down -> delete" ritual.
set -euo pipefail

HOST="" JOBDIR="" DEST="" PRUNE=0
while [ $# -gt 0 ]; do
    case "$1" in
        --host)   HOST=$2;   shift 2 ;;
        --jobdir) JOBDIR=$2; shift 2 ;;
        --dest)   DEST=$2;   shift 2 ;;
        --prune)  PRUNE=1;   shift ;;
        *) echo "unknown arg: $1" >&2; exit 1 ;;
    esac
done
[ -n "$HOST" ] && [ -n "$JOBDIR" ] && [ -n "$DEST" ] || {
    echo "usage: ./pull.sh --host HOST --jobdir DIR --dest DIR [--prune]" >&2; exit 1; }

mkdir -p "$DEST"
echo "==> rsync ${HOST}:${JOBDIR}/polys/ -> ${DEST}/"
rsync -avz --partial --progress "${HOST}:${JOBDIR}/polys/" "${DEST}/"

if [ "$PRUNE" -eq 1 ]; then
    # TODO Phase 2: verify each .ms.zst sha256 against the server manifest *before* pruning;
    # poly-server prune then deletes only blobs confirmed downloaded (marked 'archived').
    echo "==> pruning server copies confirmed-present locally"
    ssh "$HOST" "python3 -m polyserver prune --jobdir '${JOBDIR}' --confirm"
fi

echo "==> done. Optimize with:  cd ~/msieve-s && ./nfs_optimize.sh ...   # against ${DEST}"

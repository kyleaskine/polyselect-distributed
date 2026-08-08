#!/usr/bin/env bash
# Copy a local msieve corpus and its input into ~/msieve-s.
#
#   ./copy-poly.sh WORKDIR [--delete]
#
# WORKDIR may be a start number (for example, 1464), a child name of
# polylocal-work, or a path to a work directory.  A start number is resolved
# through the aliquot-tracker feed, just as polylocal resolves it. --delete
# removes that work directory only after both files have been copied.
set -euo pipefail

usage() {
    echo "usage: $0 WORKDIR [--delete]" >&2
    exit 2
}

[ "$#" -ge 1 ] && [ "$#" -le 2 ] || usage
workdir_arg=$1
delete=0
if [ "$#" -eq 2 ]; then
    [ "$2" = "--delete" ] || usage
    delete=1
fi

script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
config_file=${POLYLOCAL_CONFIG:-$script_dir/polylocal.ini}

# Match polylocal's config-file behavior.  Relative workdir values are
# relative to the caller's current directory, as they are for polylocal.
configured_work_root=polylocal-work
if [ -f "$config_file" ]; then
    configured_work_root=$(awk -F= '
        /^[[:space:]]*workdir[[:space:]]*=/ {
            value=$2
            sub(/^[[:space:]]*/, "", value)
            sub(/[[:space:]]*$/, "", value)
            print value
            exit
        }
    ' "$config_file")
    configured_work_root=${configured_work_root:-polylocal-work}
fi
if [[ "$configured_work_root" = /* ]]; then
    work_root=$(realpath -m -- "$configured_work_root")
else
    work_root=$(realpath -m -- "$PWD/$configured_work_root")
fi

if [[ "$workdir_arg" == */* ]]; then
    workdir_candidate=$workdir_arg
else
    workdir_candidate=$work_root/$workdir_arg
fi

if [ -e "$workdir_candidate" ]; then
    [ ! -L "$workdir_candidate" ] || {
        echo "error: refusing to use a symlink as a work directory: $workdir_candidate" >&2
        exit 1
    }
    workdir=$(realpath -e -- "$workdir_candidate") || {
        echo "error: work directory not found: $workdir_arg" >&2
        exit 1
    }
fi

# polylocal stores runs by sequenceId, while the convenient operator-facing
# identifier is the aliquot start number (AS1464). Resolve numeric arguments
# only when there is no local directory with that name.
if [ ! -d "${workdir:-}" ] && [[ "$workdir_arg" =~ ^[0-9]+$ ]]; then
    tracker_url=${TRACKER_URL:-}
    if [ -z "$tracker_url" ] && [ -f "$config_file" ]; then
        tracker_url=$(awk -F= '
            /^[[:space:]]*tracker_url[[:space:]]*=/ {
                value=$2
                sub(/^[[:space:]]*/, "", value)
                sub(/[[:space:]]*$/, "", value)
                print value
                exit
            }
        ' "$config_file")
    fi
    [ -n "$tracker_url" ] || {
        echo "error: no local work directory for $workdir_arg and tracker_url is not configured" >&2
        exit 1
    }

    candidate_json=$(mktemp)
    trap 'rm -f -- "$candidate_json"' EXIT
    curl --fail --silent --show-error --max-time 30 \
        "$tracker_url/api/gnfs-candidates?needsPolynomial=true" > "$candidate_json" || {
        echo "error: could not query tracker at $tracker_url" >&2
        exit 1
    }
    sequence_id=$(python3 - "$candidate_json" "$workdir_arg" <<'PY'
import json
import sys

with open(sys.argv[1]) as f:
    payload = json.load(f)
wanted = sys.argv[2]
for candidate in payload.get("data", {}).get("candidates", []):
    if str(candidate.get("startNumber")) == wanted:
        print(candidate["sequenceId"])
        break
else:
    raise SystemExit(1)
PY
    ) || {
        echo "error: tracker has no candidate for start number $workdir_arg" >&2
        exit 1
    }
    workdir_candidate=$work_root/$sequence_id
    [ -d "$workdir_candidate" ] || {
        echo "error: work directory not found: $workdir_candidate" >&2
        exit 1
    }
    [ ! -L "$workdir_candidate" ] || {
        echo "error: refusing to use a symlink as a work directory: $workdir_candidate" >&2
        exit 1
    }
    workdir=$(realpath -e -- "$workdir_candidate")
    echo "resolved AS$workdir_arg to $sequence_id"
fi

[ -n "${workdir:-}" ] || {
    echo "error: work directory not found: $workdir_arg" >&2
    exit 1
}
[ "$(dirname -- "$workdir")" = "$work_root" ] &&
[ "$(basename -- "$workdir")" != "$(basename -- "$work_root")" ] || {
    echo "error: work directory must be directly inside $work_root" >&2
    exit 1
}
[ -d "$workdir" ] || {
    echo "error: work directory not found: $workdir" >&2
    exit 1
}

ms_file=$workdir/msieve.dat.ms
worktodo=$workdir/worktodo.ini
[ -f "$ms_file" ] || { echo "error: missing $ms_file" >&2; exit 1; }
[ -f "$worktodo" ] || { echo "error: missing $worktodo" >&2; exit 1; }

destination=${HOME:?}/msieve-s
mkdir -p -- "$destination"
cp -- "$ms_file" "$worktodo" "$destination/"
echo "copied msieve.dat.ms and worktodo.ini to $destination"

if [ "$delete" -eq 1 ]; then
    rm -rf -- "$workdir"
    echo "deleted $workdir"
fi

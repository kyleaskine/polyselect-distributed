#!/usr/bin/env bash
# bootstrap-polyselect-client.sh — one-shot setup for a polyselect-distributed GPU worker.
#
# Clones + builds the GPU stage-1 msieve, clones this repo, sets up the Python client,
# prompts for connection settings, and writes run-client.sh.
#
#   curl -fsSL https://ecm.kyleaskine.com/bootstrap-polyselect-client.sh | bash
#
# (Sibling of ggnfs's bootstrap — named distinctly so both can live on the web server.)
set -euo pipefail

MSIEVE_REPO="https://github.com/kyleaskine/msieve-s"
CLIENT_REPO="https://github.com/kyleaskine/polyselect-distributed"
MSIEVE_DIR="msieve-s"
CLIENT_DIR="polyselect-distributed"

# Do NOT hardcode the real token here — this file is in a (public) repo; a committed token
# is world-readable and lets anyone lease/submit/pollute, and it lingers in git history.
# Inject at run time:  POLY_SERVER=http://HOST:8084 POLY_TOKEN=xxxx curl -fsSL URL | bash
# or bake the real values only into the copy you serve from the web server (not git), or just
# type them at the prompts below.
DEFAULT_SERVER="${POLY_SERVER:-http://CHANGE-ME:8084}"
DEFAULT_TOKEN="${POLY_TOKEN:-CHANGE-ME}"

# When piped via `curl ... | bash`, read prompts from the terminal, not the pipe.
prompt_tty() {
    local var=$1 msg=$2 def=$3 reply
    printf '%s [%s]: ' "$msg" "$def" > /dev/tty
    IFS= read -r reply < /dev/tty || reply=""
    printf -v "$var" '%s' "${reply:-$def}"
}

# 1. Prerequisites (clone-and-build needs the CUDA toolchain on the box).
missing=""
need() { command -v "$1" >/dev/null 2>&1 || missing="$missing $2"; }
need nvcc       "the CUDA toolkit (nvcc)"
need nvidia-smi "the NVIDIA driver (nvidia-smi)"
need gcc        build-essential
need make    build-essential
need git     git
need python3 python3
need zstd    zstd
[ -f /usr/include/gmp.h ] || [ -f /usr/include/x86_64-linux-gnu/gmp.h ] || missing="$missing libgmp-dev"
if [ -n "$missing" ]; then
    echo "error: missing prerequisites:$missing" >&2
    exit 1
fi

echo "==> polyselect-distributed worker bootstrap"

# 2. Clone + build msieve (GPU stage-1 only — the rest of msieve is unused here).
if [ -d "$MSIEVE_DIR/.git" ]; then
    git -C "$MSIEVE_DIR" pull --ff-only
else
    git clone "$MSIEVE_REPO" "$MSIEVE_DIR"
fi
if [ ! -x "$MSIEVE_DIR/msieve" ]; then
    # msieve builds with `make all CUDA=<CC>` where CC is the GPU compute capability
    # with the dot removed (RTX 5070 / sm_120 -> 120, sm_89 -> 89). Auto-detect from the
    # driver; override with CUDA_CC=NNN for heterogeneous boxes. Phase 3 will add a trimmed
    # 'make polyselect-stage1' target.
    CUDA_CC="${CUDA_CC:-$(nvidia-smi --query-gpu=compute_cap --format=csv,noheader 2>/dev/null | head -n1 | tr -d ' .')}"
    if [ -z "$CUDA_CC" ]; then
        prompt_tty CUDA_CC "GPU compute capability for the msieve build (e.g. 120)" ""
    fi
    [ -n "$CUDA_CC" ] || { echo "error: could not determine GPU compute capability; rerun with CUDA_CC=NNN" >&2; exit 1; }
    echo "==> building msieve for CUDA=$CUDA_CC (GPU stage-1)"
    ( cd "$MSIEVE_DIR" && make all CUDA="$CUDA_CC" )
fi
MSIEVE_BIN="$(cd "$MSIEVE_DIR" && pwd)/msieve"
# No COLLLIB: the client symlinks cub/ + stage1_core.ptx into each workunit dir, so msieve's
# relative defaults resolve and no --colllib/--sortlib is needed (see polyclient/msieve_runner.py).

# 3. Clone this repo + set up a lean Python env for the client.
if [ -d "$CLIENT_DIR/.git" ]; then
    git -C "$CLIENT_DIR" pull --ff-only
else
    git clone "$CLIENT_REPO" "$CLIENT_DIR"
fi
cd "$CLIENT_DIR"
python3 -m venv .venv
# shellcheck disable=SC1091
. .venv/bin/activate
pip install -q --upgrade pip
pip install -q -r requirements-client.txt

# 4. Configure + write run-client.sh.
echo
echo "==> Configure the worker (press Enter to accept defaults)"
prompt_tty CLIENT_ID "client id"    "$(hostname -s 2>/dev/null || echo worker)"
prompt_tty GPU       "gpu index"    "0"
prompt_tty SERVER    "server URL"   "$DEFAULT_SERVER"
prompt_tty TOKEN     "worker token" "$DEFAULT_TOKEN"

ABS="$(pwd)"
cat > run-client.sh <<EOF
#!/usr/bin/env bash
cd "$ABS"
. .venv/bin/activate
exec python3 -m polyclient \\
    --server-url="$SERVER" --token="$TOKEN" \\
    --msieve="$MSIEVE_BIN" \\
    --gpu="$GPU" --client-id="$CLIENT_ID"
EOF
chmod +x run-client.sh

echo
echo "==> Done. Start the worker:  $ABS/run-client.sh"

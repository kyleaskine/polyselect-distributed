# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Distributed coordinator for **GNFS polynomial selection stage 1** (`msieve -np1`) — the
GPU-bound, embarrassingly-parallel search over leading coefficients. GPU clients each run
patched msieve for one leading coefficient and upload the raw polynomials; a tiny
compute-less coordinator (a droplet) hands out coefficients and stores results.
Size/root optimization happens later, elsewhere, via the operator's existing
`~/msieve-s/nfs_optimize.sh`.

**`DESIGN.md` is the canonical spec.** Code comments cite it as `DESIGN.md §N`; read the
referenced section before changing behavior near such a comment. Status: **Phase 1
implemented**, Phase 2/3 stubbed (see "Phase boundaries" below).

## Commands

```bash
# Tests — no framework; two scripts.
python3 tests/test_db.py            # DB state machine + verify parser. Stdlib only; no deps, no GPU.
python3 tests/test_integration.py   # full client↔server over real HTTP w/ a stub msieve.
                                    # Needs runtime deps installed (below); no GPU, gmpy2 not needed.

# Install deps (for the integration test or running for real)
pip install -r requirements-server.txt -r requirements-client.txt

# Server (on the droplet). init/extend/prune are run over SSH; serve runs the worker API.
python3 -m polyserver init  --jobdir DIR --worktodo worktodo.ini \
        --coeff-list coeff_list.txt [--high-coeff-mult M] [--degree 5] [--force]
python3 -m polyserver serve  --jobdir DIR --bind 0.0.0.0 --port 8080   # single process only
python3 -m polyserver extend --jobdir DIR --coeff-list more_coeffs.txt

# Client (on a GPU box)
python3 -m polyclient --server-url http://host:8080 --token <worker-token> \
        --msieve /path/to/msieve --colllib /path/to/collision_engine.so --gpu 0
```

There is no lint/build step (pure Python + shell). `bootstrap-polyselect-client.sh`
clone-and-builds msieve on a GPU box and writes `run-client.sh`; `pull.sh` rsyncs the
corpus down. Neither test replaces the **real-msieve `coeff_list=1` validation on a GPU
box** (DESIGN.md §7) — the one remaining gate before scale-up.

## Architecture

A **workunit = one leading coefficient `C`**. Three actors (DESIGN.md §3):

- **`polyserver/`** (FastAPI, on the droplet) — worker HTTP API + read-only dashboard.
  Hands out coefficients, stores `.ms` blobs, does cheap inline verification. *No admin
  HTTP surface.*
- **`polyclient/`** (on GPU boxes) — lease a coeff → run msieve → upload. One msieve
  process per GPU.
- **External** — operator pulls the corpus to a real box and runs `~/msieve-s/nfs_optimize.sh`.

### The producer/consumer contract is at the artifact boundary

Server gives `(job, C)`; client returns a `.ms` file of raw polynomials in **CADO format**
(blank-line-separated `n: / c_i: / Y1: / Y0:` records, every `c_d == C`). *Whatever*
produces that file — full msieve now, a trimmed binary later — is a drop-in swap with **no
server/protocol change**. Keep this boundary clean.

### The msieve invocation (`polyclient/msieve_runner.py`)

Per leased `C`, in a per-workunit workdir: write `worktodo.ini` (**bare decimal N**, not
INI) and `coeff_list.txt` (the one coefficient), then run
`msieve -g <gpu> -np1 -nps "coeff_list=1 high_coeff_mult=M collengine=gerbicz colllib=<so>"`.
**`coeff_list=1` is load-bearing**: it makes stage 1 read coefficients straight from
`coeff_list.txt` and sieve each directly (`stage1.c:487-521`), bypassing the `find_next_ad`
range enumerator → one line = exactly one `a_d`. `min_coeff=max_coeff=C` does **not** work
(DESIGN.md §2, §7). **`high_coeff_mult` is inert in coeff_list mode** — it only feeds the
range enumerator, which this path skips; per-coeff bounds come from `stage1_bounds_update`
(derived from the actual `a_d`), so `--high-coeff-mult` is optional and the client omits it
when unset. Output is `<workdir>/msieve.dat.ms`. msieve runs in its own process
group so cancel can SIGTERM/SIGKILL the whole GPU job.

### Workunit state machine (`polyserver/db.py`)

`available → leased → submitted → verified`, with failure loopback (`attempt_count++` → back
to `available`, or `→ poisoned` past `--max-attempts`). A daemon sweep thread requeues
expired leases. **Phase 1: `submitted` is terminal** (no verifier advances it to `verified`
yet). Invariants when touching the DB:

- **`coeff` is TEXT and may exceed 64-bit. Never `CAST` it.** Lease ordering is
  `ORDER BY length(coeff), coeff` (bignum-safe).
- **Single-process by design.** One shared sqlite connection (`check_same_thread=False`,
  `isolation_level=None`, WAL) is serialized by one `threading.Lock` in `app.py`; lease's
  SELECT-then-UPDATE atomicity *depends on that lock*. **Do not run multiple uvicorn
  workers.** Every `db.*` call in `app.py` is wrapped in `with lock:`.
- `/submit` is **idempotent**: `db.submit` returns `accepted`/`duplicate`(200, lost-ACK
  retry)/`conflict`(409)/`unknown`(404). The client retries 5xx/transport but never a 4xx
  (the server already handled it) — completed `.ms` work is never dropped.

### Storage (`schema.sql`)

SQLite holds **metadata only**: `meta` (N, degree, multiplier, worker token…), `workunits`,
`submissions`. Raw blobs live on disk at `polys/<coeff>-<sha>.ms.zst` — content-addressed,
zstd-compressed, referenced by path, **never in the DB**.

### Verification (`polyserver/verify.py`)

- **Phase 1 (implemented, inline):** during the sha-verify decompress pass, `/submit`
  parses the first K records (`--spotcheck-k`, default 50) and asserts `c_d == C`. Fail →
  blob deleted + workunit requeued/poisoned + HTTP 422. An empty result (0 bytes) is legal.
- **Phase 2 (TODO):** background worker (concurrency 1, own connection) adds the algebraic
  mod-N homomorphism check via gmpy2, advancing `submitted → verified`.

### Auth (DESIGN.md §8)

Low-privilege **worker token** (Bearer, plain HTTP, baked into bootstrap, rotatable) gates
`lease`/`submit`/`release`/`health`/`stats`. Everything privileged (`init`/`extend`/`prune`,
`pull`) goes over the operator's **SSH key** to the droplet — there are no admin HTTP
endpoints by design. Don't add one; add a `polyserver` subcommand run over SSH instead.

## Phase boundaries (what's a stub)

`prune` (CLI + `pull.sh`'s `--prune`) raises/`TODO` — Phase 2. The background mod-N
verifier is Phase 2. A trimmed `make polyselect-stage1` build target is Phase 3. The
`comp_sha256` column and `archived` flag exist now to feed the Phase-2 prune manifest.
When adding to these, keep the artifact-boundary contract and single-process invariant intact.

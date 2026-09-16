# polyselect-distributed

Distributed coordinator for **GNFS polynomial-selection stage 1** (`msieve -np1`) — the
GPU-bound search over leading coefficients. GPU clients each run patched msieve for one
leading coefficient and upload the raw polynomials; a tiny coordinator (a droplet, no
compute) hands out coefficients and collects results. Size/root optimization runs later,
on a real box, via the existing `~/msieve-s/nfs_optimize.sh`.

**Design & contracts:** see [DESIGN.md](DESIGN.md). This is an early **scaffold** — most
handlers are Phase-1 stubs (`NotImplementedError` / HTTP `501`). Shape and contracts
first; logic next.

## Layout

- `polyserver/` — FastAPI worker API + `init`/`extend`/`serve`/`prune` CLI (runs on the droplet).
- `polyclient/` — GPU worker: lease → run msieve via `coeff_list` → upload.
- `polylocal/` — local one-shot runner: fetch a composite from aliquot-tracker → run msieve
  locally → (optionally) optimize and submit the best polynomial back. No coordinator needed.
- `schema.sql` — SQLite metadata schema (raw `.ms` blobs live on disk, not in the DB).
- `bootstrap-polyselect-client.sh` — one-shot GPU-worker setup (clone+build msieve, configure).
- `pull.sh` — pull the corpus to a workstation over SSH (+ optional prune).
- `copy-poly.sh` — resolve an aliquot start number and copy its local corpus and `worktodo.ini` into `~/msieve-s`.

## Quickstart (planned — Phase 1 in progress)

```bash
# on the droplet
python3 -m polyserver init --jobdir /srv/polyjob \
    --worktodo worktodo.ini --coeff-list coeff_list.txt \
    --high-coeff-mult 60060 --degree 5
python3 -m polyserver serve --jobdir /srv/polyjob --bind 0.0.0.0 --port 8080

# on each GPU box
curl -fsSL https://ecm.kyleaskine.com/bootstrap-polyselect-client.sh | bash
./polyselect-distributed/run-client.sh

# on the workstation, when you want to optimize
./pull.sh --host droplet --jobdir /srv/polyjob --dest ~/corpus --prune
cd ~/msieve-s && ./nfs_optimize.sh ...   # run against ~/corpus
```

## Local one-shot runner (`polylocal`)

For the common case — a single, not-huge composite — you don't need the coordinator at all.
`polylocal` runs the whole thing on one GPU+CPU box: it pulls a composite that needs a
polynomial straight from [aliquot-tracker](../aliquot-tracker)'s public
`gnfs-candidates` feed, runs the msieve coefficient search locally, and (opt-in) optimizes
and submits the best polynomial back. It's "the manual msieve dance, scripted."

**Configuration.** Copy `polylocal.ini.example` → `polylocal.ini` (gitignored) and fill in your
tracker URL, a submit credential, and the msieve / nfs_optimize paths, so you don't pass them
every run. The credential is either **`api_key`** (your personal API key, sent as `X-Api-Key` —
your tracker account must be an admin) or **`internal_key`** (the shared server `INTERNAL_API_KEY`,
sent as `X-Internal-Key`); set one. Precedence for each setting is **CLI flag > environment
variable > `polylocal.ini` > built-in default**.

```bash
cp polylocal.ini.example polylocal.ini    # edit once with your values

# produce a corpus for an aliquot sequence (runs msieve locally, stops at the corpus):
python3 -m polylocal --start-number 552

# go end-to-end: optimize locally and submit the winner back to the tracker:
python3 -m polylocal --next --optimize --submit

# submit a polynomial you optimized by hand — a single .p OR a whole nfs_optimize output
# (polylocal picks the best across msieve `# … e … rroots` and CADO `# side 1 MurphyE=`):
python3 -m polylocal --sequence-id <uuid> --poly-file best.p --submit
```

No coeff_list to curate: msieve runs in **range mode**, and `min_coeff` / `high_coeff_mult` /
`num_polys` default from the composite's **digit count**, stopping once `num_polys` raw
polynomials have been found:

| Digits | `min_coeff` = `high_coeff_mult` | `num_polys` |
|---|---|---|
| `< C145` | — (too small; run msieve directly) | — |
| `< C159` | 420 | 400,000 |
| `< C173` | 2,520 | 800,000 |
| `< C187` | 27,720 | 1,200,000 |
| `< C201` | 138,600 | 1,600,000 |
| `≥ C201` | set `--min-coeff --high-coeff-mult --num-polys` yourself | |

Override any tier value with `--min-coeff` / `--high-coeff-mult` / `--num-polys` (all three to
run a composite above the table). `collengine=gerbicz` is always used.

**Resuming.** msieve appends to `msieve.dat.ms`, so a run cut short can be continued with
`--resume`: it counts the polys already in `<workdir>/<sequenceId>/msieve.dat.ms`, restarts at
`min_coeff = last leading coeff + 1` (msieve rounds that up to the next multiple of
`high_coeff_mult`), and searches only for the rest of `num_polys`. If the target is already met
it skips msieve. Without `--resume`, polylocal refuses to run into a non-empty corpus, since that
would repeat the search and duplicate its polys. On a CUDA build a single Ctrl-C finishes the
coefficient in flight, so resume loses nothing. After a second (hard) Ctrl-C — or on a CPU-only
msieve, where even the first Ctrl-C aborts the coefficient immediately — the last coefficient is
only partly searched, and resume does not revisit it.

```bash
python3 -m polylocal --start-number 1512 --resume
```

> **Requires the patched `msieve-s`.** The `num_polys=` stop is a small patch to the
> `kyleaskine/msieve-s` fork (stage 1: count emitted polys on the single-thread stage-2 pool,
> trip the existing soft-stop at the target; `num_polys=0` = unchanged). An **unpatched** msieve
> silently ignores `num_polys=` and would search the whole range — rebuild `msieve-s` before
> using range mode.

Selector is one of `--start-number` (the AS seed) / `--sequence-id` / `--next`. `TRACKER_URL`,
`INTERNAL_API_KEY`, `MSIEVE`, and `NFS_OPTIMIZE` work as env fallbacks. `--dry-run` fetches and
builds the msieve command (and validates a `--poly-file`) without running msieve or POSTing.
This is the first step toward the coordinator becoming a broker between the tracker and
compute — see the plan for the deferred phases.

## Trust model

Worker traffic is plain HTTP + a low-privilege **worker token** on a private/semi-trusted
network (same model as `ggnfs-distributed`). Everything privileged — `init`/`extend`/
`prune` and `pull` — goes over your **SSH key** to the droplet; there is no admin HTTP
surface. See DESIGN.md §8.

## Tests

No framework required — two scripts:

- **`python3 tests/test_db.py`** — DB state machine + verify parser. Stdlib only; no deps, no GPU.
- **`python3 tests/test_integration.py`** — full client↔server over real HTTP with a stub
  msieve (lease → upload → sha+`c_d` verify → idempotency → 204). Needs the runtime deps
  (`pip install -r requirements-server.txt -r requirements-client.txt`; `gmpy2` not required). No GPU.
- **`python3 tests/test_polylocal.py`** — `polylocal` end-to-end against a stub tracker + stub
  msieve + stub optimizer (fetch → select → corpus → best-poly extract/validate → submit).
  Needs `httpx`; no GPU, no real msieve/CADO.

Neither replaces the real-msieve `coeff_list=1` validation on a GPU box (DESIGN.md §7).

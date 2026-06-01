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
- `schema.sql` — SQLite metadata schema (raw `.ms` blobs live on disk, not in the DB).
- `bootstrap-polyselect-client.sh` — one-shot GPU-worker setup (clone+build msieve, configure).
- `pull.sh` — pull the corpus to a workstation over SSH (+ optional prune).

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

## Trust model

Worker traffic is plain HTTP + a low-privilege **worker token** on a private/semi-trusted
network (same model as `ggnfs-distributed`). Everything privileged — `init`/`extend`/
`prune` and `pull` — goes over your **SSH key** to the droplet; there is no admin HTTP
surface. See DESIGN.md §8.

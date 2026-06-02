# polyselect-distributed — Design

Distributed coordinator for **GNFS polynomial-selection stage 1** (`msieve -np1`) — the
GPU-bound, embarrassingly-parallel search over leading coefficients. Sibling to
`ggnfs-distributed` (which distributes lattice sieving).

Status: **design, pre-code.** This document is for review; nothing is built yet.

## 1. The work, and why it splits the way it does

NFS polynomial selection has phases with very different cost profiles:

| Phase | Cost | Where it runs here |
|---|---|---|
| **Stage 1** (`-np1`): collision search per leading coefficient | GPU-bound, ~minutes/coeff, embarrassingly parallel | **distributed to GPU clients** |
| **Size opt** (CADO `sopt`): translate/rotate to minimize norm | CPU, ~1–5 s/poly, parallel | operator workstation, on command |
| **Root opt** (msieve/CADO `ropt`): maximize Murphy E on top-N | CPU, slow | operator workstation, on command |

A **workunit = one leading coefficient `C`**. On an RTX 5070, coeff 60060 ≈ 10 min and
yields ~217K raw polynomials (confirmed: the first 20,000 polys in a real
`msieve.dat.ms` are all `c5: 60060`). The client runs only stage 1 + a cheap expand;
everything CPU-heavy happens later, elsewhere.

The patched msieve (`~/msieve-s`) makes the client output clean: with `-np1 -nps`, the
modified `poly_sizeopt_run` (`gnfs/poly/stage2/stage2.c`) takes each stage-1 hit, does
`pol_expand`, and writes the **raw expanded polynomial in CADO input format**
(`n: / c_i: / Y1: / Y0:`) to `<savefile>.ms` — *without* the actual size optimization. So
the client emits exactly the corpus CADO `sopt` consumes.

## 2. The work is a curated coefficient list, not a range

Stage 1 does **not** test every multiple of the high-coeff multiplier `M`.
`find_next_ad` (`stage1.c:314`) only emits `a_d = M·k` where `k` is smooth enough to have
many projective roots. The operator's existing `~/msieve-s/coeff_list.txt` is precisely
this: a hand-curated highly-composite list (1260, 2520, …, 60060, …, 1021020, 2042040 …),
not consecutive multiples.

**So a job's work is an explicit list of leading coefficients.** MVP: the operator hands
the server that list at `init` (the `coeff_list.txt` artifact). The server tracks/leases
each coefficient as a workunit. *Auto-generating the list from a range + smoothness
filter is a later convenience, not MVP.*

## 3. Topology

```
  GPU clients (rented boxes)            droplet (no compute)            operator workstation
  ┌────────────────────────┐          ┌────────────────────────┐      ┌──────────────────────────┐
  │ poly-client (Python)    │  lease C │ poly-server (FastAPI)   │      │  SSH (private key)       │
  │  └ subprocess: msieve    │ ───────▶ │  • hand out coeffs      │      │   • poly-server init/    │
  │    -np1 -nps  (CUDA)     │          │  • store .ms (zstd)     │◀─────│     extend/prune (on box)│
  │    via coeff_list.txt    │ submit   │  • sample-verify (bg)   │ ssh  │   • pull = rsync down    │
  │  └ zstd msieve.dat.ms    │ ───────▶ │  • coverage dashboard   │      │                          │
  └────────────────────────┘          └────────────────────────┘      │  then: nfs_optimize.sh   │
        worker token, plain HTTP         worker API + read-only /stats   │  (CADO sopt→ropt→best)   │
                                                                        └──────────────────────────┘
```

The droplet **never runs sopt/ropt** (single-core, ~no compute). Optimization = operator
pulls the corpus to a real box and runs the existing `~/msieve-s/nfs_optimize.sh`, on
command. **Privileged ops go over SSH** (next section), so there is no admin HTTP surface.

## 4. Components

- **poly-server** (FastAPI, on the droplet) — HTTP worker API: lease coefficients,
  receive + store `.ms` blobs, sampled verification in a background queue, read-only
  coverage dashboard. Plus server-side subcommands run **on the box over SSH**: `init`,
  `extend`, `prune`. SQLite for metadata, files on disk for blobs.
- **poly-client** (Python, on GPU boxes) — lease a coefficient, write `worktodo.ini` +
  `coeff_list.txt`, run `msieve -np1 -nps`, capture `msieve.dat.ms`, zstd-compress,
  upload. **One msieve process per GPU.** Drain/cancel on Ctrl-C (kills the msieve child).
- **pull** (shell script, on the workstation) — `rsync` the droplet's `polys/` down over
  SSH using the operator's key; optionally trigger `prune` after a verified transfer.
- **External optimize** (unchanged) — `~/msieve-s/nfs_optimize.sh` against the pulled corpus.

## 5. Workunit state machine

`available → leased → submitted → verified`, with failure loopback
(`leased`/`submitted` → `available`, `attempt_count++`) and `→ poisoned` after
`--max-attempts`. A lease-expiry sweep requeues abandoned coefficients. The set of
workunits is the job's coefficient list; `extend` appends more coefficients without a
restart. Same spirit as ggnfs.

## 6. HTTP API (JSON) — worker-only

Authenticated by the **worker token** (low-privilege; see §8). No admin endpoints.

- `POST /lease` → `{ workunit_id, coeff, job:{N, degree, high_coeff_mult, deadline, collengine, colllib_hint}, lease_seconds }`
- `POST /submit` — body: zstd'd `.ms`; headers: `workunit_id`, `sha256`, `poly_count`, `X-Compression: zstd` → `{ ok, stored_sha }`
- `POST /release` — voluntary lease return on cancel
- `GET  /health`
- `GET  /stats` — dashboard data (read-only)
- `GET  /` — dashboard HTML (token in URL, like ggnfs)

## 7. The msieve invocation contract (client)

Per leased coefficient `C`, in a per-workunit workdir containing `worktodo.ini` (the
**bare decimal N** — confirmed format) and `coeff_list.txt` (the assigned coefficient,
one per line):

```
./msieve -g <gpu_id> -np1 -nps "coeff_list=1 high_coeff_mult=M collengine=gerbicz colllib=<path>"
```

- **`coeff_list=1`** makes stage 1 read coefficients from `coeff_list.txt` and sieve each
  directly (`stage1.c:487–521`), bypassing the `find_next_ad` range enumerator — so one
  line = exactly one `a_d`, deterministically. (`min_coeff=max_coeff=C` does **not** work:
  `find_next_ad` only emits smooth `a_d ≤ max_coeff` and the single candidate `k=1` is
  threshold-fragile.) `high_coeff_mult=M` is still passed for internal bounds.
- `N` comes from `worktodo.ini` in the workdir; degree + norms auto-derive from `N`
  (consistent across clients since `N` is fixed for the job).
- Output: `<workdir>/msieve.dat.ms` (CADO-format raw polys, every `c_d == C`). Client
  computes sha256, zstd-compresses, uploads.

**Confirmed:** `worktodo.ini` = bare N; `c_d` pinned per coefficient.
**To verify in Phase 1:** a single-line `coeff_list.txt` sieves exactly that one
coefficient end-to-end.

**Artifact-boundary contract:** server gives `(job, C)`, client returns a `.ms`. Whatever
produces that file — full msieve now, a trimmed `polyselect-stage1` binary later, an
extracted lib someday — is a drop-in swap with **no server/protocol change**.

## 8. Security / auth — worker token + SSH key

The worker token is distributed widely (baked into `bootstrap-polyselect-client.sh`, fetched onto
every rented box like ggnfs hardcodes its token), so it is **low-privilege**: `lease`,
`submit`, `release`, `health`, `stats`. Rotatable.

Everything privileged uses the operator's **SSH key** to the droplet — no admin HTTP
token, no admin endpoints, no API-over-tunnel:

- `init`, `extend`, `prune` run as `poly-server` subcommands **on the droplet** (the
  ggnfs pattern), invoked over SSH.
- `pull` is `rsync` over SSH from the workstation.

This is strictly simpler and more secure than a second bearer token (SSH key ≫ token in
cleartext HTTP), and it matches how the operator already runs the droplet (web-console
SSH tunnel + sftp, private keys on hand). Worker traffic stays on the ggnfs trust model
(plain HTTP + worker token, private/semi-trusted).

## 9. Storage

SQLite (WAL) on the droplet, **metadata only**:
- `job`/`meta` — N, degree, multiplier, deadline, collengine, worker token, how the
  coefficient list was sourced.
- `workunits` — coeff, state, attempt_count, lease_expires, client_id.
- `submissions`/`blobs` — workunit_id, sha256 (raw .ms), comp_sha256 (stored .zst),
  bytes, poly_count, verify_status, `archived` flag, path.

Blobs on disk: `polys/<coeff>-<sha>.ms.zst`, content-addressed, referenced by path.
**Never in the DB.**

**Single-process by design:** one shared SQLite connection guarded by an in-process lock
(lease's SELECT-then-UPDATE relies on it), so run exactly one uvicorn worker (the default).

## 10. Verification

**Phase 1 — inline, cheap (implemented).** `/submit` already stream-decompresses the
upload to verify its sha256, so in the same path it parses the first `K` records
(`--spotcheck-k`, default 50; 0 disables) and asserts each has `c_d == C` (the assigned
coefficient). This reads only a tiny decompressed prefix and catches the realistic failure
— a mismatched client build emitting the wrong coefficient or garbage — before it pollutes
the corpus. Pass → submission `verify_status='passed'`; fail → blob deleted, workunit
requeued (`attempt_count++`) → `poisoned` past the cap, client gets `422`.

**Phase 2 — background, thorough.** A worker (concurrency 1, off the upload path)
random-reservoir-samples across the whole file and adds the algebraic homomorphism check
`Σ_{i=0..d} c_i · (−Y0)^i · Y1^(d−i) ≡ 0 (mod N)` (gmpy2), advancing `submitted → verified`.
A handful of bignum mults per poly; cheaper than ggnfs's norm check.

## 11. Pull / prune lifecycle

`pull` (workstation shell script):
1. `rsync -avz droplet:<jobdir>/polys/ <dest>/` over SSH (incremental, resumable, secure
   via the operator's key).
2. Verify sha256 of each downloaded `.ms.zst` against the server's manifest.
3. Optionally `ssh droplet 'poly-server prune --jobdir … --confirm'` to delete only blobs
   confirmed downloaded (marked `archived`). **Prune is dry-run unless `--confirm`** and
   never touches un-pulled data.

Disk is comfortable — droplets have 25 GB and a job's corpus is rarely past ~2 GB even
uncompressed — so prune is **workflow convenience** (automating the manual copy-down-then-
delete ritual), not a capacity safeguard. Blobs always stored zstd-compressed.

## 12. Bootstrap (`bootstrap-polyselect-client.sh`)

Like ggnfs's, adapted for GPU + clone-and-build:
1. Check prereqs: CUDA toolkit (`nvcc`), gcc/make, git, python3, zstd, gmp.
2. Detect GPU arch (compute capability via `nvidia-smi`).
3. Clone `github.com/kyleaskine/msieve-s`, build the GPU poly path
   *(later: a trimmed `make polyselect-stage1` target — faster build, small binary)*.
4. Clone `polyselect-distributed`, set up the client.
5. Prompt for server URL + worker token (defaults baked like ggnfs), write `run-client.sh`.

## 13. Phased build plan

- **Phase 1 — MVP coordinator + client (implemented; live HTTP path pending a deps-installed run).**
  Server: `init` (explicit coefficient list; `--force` reinit), `extend`, `lease`,
  idempotent streaming sha-verified `submit` + cheap inline `c_d`/parseability check,
  store, coverage dashboard, lease-expiry sweep. Client: wrap full msieve via `coeff_list`,
  streaming upload, drain/cancel, retry-through-outage. `bootstrap-polyselect-client.sh`.
- **Phase 2 — durability + ops.** Background mod-N verifier; `pull` (rsync) + `prune`
  subcommand (uses the `comp_sha256` manifest); disk gauge.
- **Phase 3 — leaner client.** Trimmed `make polyselect-stage1` target.
- **Future.** msieve-free client (true stage-1 extraction); server-side quality
  leaderboard *if* the server ever gets compute; multi-GPU per box; lease heartbeats.

## 14. Deferred / open

- **OPEN — coefficient-list sourcing.** MVP ingests an explicit list. Later: auto-generate
  smooth `a_d` over a `[min,max]` range (mirror `find_next_ad`'s smoothness sieve, or a
  cheap msieve enumerate-only pass) so the operator doesn't curate by hand.
- **Future granularity (deferred).** Small N: *bundle* several `a_d` per workunit — free
  via a multi-line `coeff_list.txt`. Large N (hours per `a_d`): *split* below one `a_d` by
  partitioning the special-q range within a coefficient — needs investigating whether
  stage-1 exposes a special-q-range knob.
- Lease heartbeats for long coefficients (ggnfs's open item too).

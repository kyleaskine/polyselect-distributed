-- polyselect-distributed — poly-server SQLite schema (WAL mode).
-- Metadata only. Raw .ms polynomial blobs live on disk under <jobdir>/polys/,
-- content-addressed + zstd-compressed (DESIGN.md §9). One job per server.

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
-- keys: schema_version, n, degree, high_coeff_mult, deadline, collengine,
--       worker_token, created_at, coeff_source

CREATE TABLE IF NOT EXISTS workunits (
    id            TEXT PRIMARY KEY,                  -- 'wu-<coeff>'
    coeff         TEXT NOT NULL UNIQUE,              -- leading coefficient, decimal (may exceed 64-bit)
    state         TEXT NOT NULL DEFAULT 'available', -- available|leased|submitted|verified|poisoned
    attempt_count INTEGER NOT NULL DEFAULT 0,
    client_id     TEXT,
    lease_expires INTEGER,                           -- unix epoch seconds
    created_at    INTEGER NOT NULL,
    updated_at    INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_wu_state ON workunits(state, lease_expires);

CREATE TABLE IF NOT EXISTS submissions (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    workunit_id   TEXT NOT NULL REFERENCES workunits(id),
    sha256        TEXT NOT NULL,                     -- sha256 of the *uncompressed* .ms
    bytes         INTEGER NOT NULL,                  -- stored (compressed) size
    poly_count    INTEGER,                           -- client-reported; corrected by the verifier
    verify_status TEXT NOT NULL DEFAULT 'pending',   -- pending|passed|failed|skipped
    archived      INTEGER NOT NULL DEFAULT 0,        -- 1 once pulled + sha-verified (prunable)
    path          TEXT NOT NULL,                     -- 'polys/<coeff>-<sha>.ms.zst'
    client_id     TEXT,
    submitted_at  INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sub_verify   ON submissions(verify_status);
CREATE INDEX IF NOT EXISTS idx_sub_archived ON submissions(archived);

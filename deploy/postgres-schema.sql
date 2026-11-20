-- MyDrive Commercial Edition — PostgreSQL 16 Production Schema
-- Run via: psql "$DATABASE_URL" -f deploy/postgres-schema.sql

CREATE TABLE IF NOT EXISTS users (
    user_id         TEXT PRIMARY KEY,
    username        TEXT UNIQUE NOT NULL,
    password_hash   TEXT NOT NULL,
    email           TEXT DEFAULT '',
    phone           TEXT DEFAULT '',
    quota_bytes     BIGINT DEFAULT 10737418240,
    used_bytes      BIGINT DEFAULT 0,
    created_at      BIGINT NOT NULL
);

CREATE TABLE IF NOT EXISTS files (
    identity         TEXT PRIMARY KEY,
    parent           TEXT NOT NULL,
    name             TEXT NOT NULL,
    path             TEXT NOT NULL,
    size             BIGINT DEFAULT 0,
    mime_type        TEXT DEFAULT 'application/octet-stream',
    is_dir           BOOLEAN DEFAULT FALSE,
    content_identity TEXT DEFAULT '',
    store_type       INTEGER DEFAULT 0,
    create_ts        BIGINT DEFAULT 0,
    update_ts        BIGINT DEFAULT 0,
    delete_ts        BIGINT DEFAULT 0,
    deleted          BOOLEAN DEFAULT FALSE,
    trashed          BOOLEAN DEFAULT FALSE,
    shared           BOOLEAN DEFAULT FALSE,
    hidden           BOOLEAN DEFAULT FALSE,
    starred          BOOLEAN DEFAULT FALSE,
    locked           BOOLEAN DEFAULT FALSE,
    version          INTEGER DEFAULT 1,
    sort_name        TEXT DEFAULT '',
    nodes            INTEGER DEFAULT 0,
    desc_files       INTEGER DEFAULT 0,
    desc_dirs        INTEGER DEFAULT 0,
    user_id          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_files_parent ON files(user_id, parent, deleted, trashed);
CREATE INDEX IF NOT EXISTS idx_files_path   ON files(user_id, path);
CREATE INDEX IF NOT EXISTS idx_files_name   ON files(user_id, name);

CREATE TABLE IF NOT EXISTS chunks (
    cid          TEXT PRIMARY KEY,
    size         BIGINT NOT NULL,
    ref_count    INTEGER DEFAULT 1,
    create_ts    BIGINT DEFAULT 0,
    inline_data  BYTEA,
    format       INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS file_chunks (
    file_identity TEXT NOT NULL REFERENCES files(identity) ON DELETE CASCADE,
    chunk_index   INTEGER NOT NULL,
    cid           TEXT NOT NULL REFERENCES chunks(cid),
    start_byte    BIGINT NOT NULL,
    end_byte      BIGINT NOT NULL,
    size          BIGINT NOT NULL,
    PRIMARY KEY (file_identity, chunk_index)
);

CREATE TABLE IF NOT EXISTS fast_lookup (
    identity         TEXT PRIMARY KEY,
    sha1             TEXT NOT NULL,
    head_sha1        TEXT NOT NULL,
    file_size        BIGINT NOT NULL,
    content_identity TEXT NOT NULL,
    name             TEXT DEFAULT '',
    create_ts        BIGINT DEFAULT 0,
    user_id          TEXT NOT NULL,
    UNIQUE (sha1, file_size, user_id)
);
CREATE INDEX IF NOT EXISTS idx_fast_sha1 ON fast_lookup(sha1, file_size);

CREATE TABLE IF NOT EXISTS chunk_tiers (
    cid            TEXT PRIMARY KEY,
    raw_size       BIGINT NOT NULL DEFAULT 0,
    stored_size    BIGINT NOT NULL DEFAULT 0,
    compressed     BOOLEAN NOT NULL DEFAULT FALSE,
    in_local       BOOLEAN NOT NULL DEFAULT TRUE,
    in_cold        BOOLEAN NOT NULL DEFAULT FALSE,
    last_access_ts BIGINT NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_chunk_tiers_lru ON chunk_tiers(in_local, last_access_ts);

CREATE TABLE IF NOT EXISTS file_versions (
    id               BIGSERIAL PRIMARY KEY,
    file_identity    TEXT NOT NULL,
    version          INTEGER NOT NULL,
    content_identity TEXT NOT NULL,
    size             BIGINT NOT NULL,
    chunks_json      JSONB NOT NULL,
    create_ts        BIGINT NOT NULL,
    note             TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS user_memberships (
    user_id    TEXT PRIMARY KEY,
    plan       TEXT NOT NULL DEFAULT 'free',
    expire_ts  BIGINT NOT NULL DEFAULT 0,
    updated_at BIGINT NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS redeem_codes (
    code          TEXT PRIMARY KEY,
    plan          TEXT NOT NULL,
    duration_days INTEGER NOT NULL,
    used_by       TEXT DEFAULT '',
    used_at       BIGINT DEFAULT 0,
    created_at    BIGINT NOT NULL
);

CREATE TABLE IF NOT EXISTS payment_orders (
    order_no      TEXT PRIMARY KEY,
    user_id       TEXT NOT NULL,
    plan          TEXT NOT NULL,
    duration_days INTEGER NOT NULL,
    amount_cents  INTEGER NOT NULL,
    currency      TEXT NOT NULL DEFAULT 'CNY',
    status        TEXT NOT NULL DEFAULT 'pending',
    provider      TEXT DEFAULT 'generic',
    created_at    BIGINT NOT NULL,
    paid_at       BIGINT DEFAULT 0
);

CREATE TABLE IF NOT EXISTS blocked_cids (
    cid        TEXT PRIMARY KEY,
    reason     TEXT DEFAULT '',
    blocked_at BIGINT NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_logs (
    id      BIGSERIAL PRIMARY KEY,
    ts      BIGINT NOT NULL,
    user_id TEXT DEFAULT '',
    action  TEXT NOT NULL,
    target  TEXT DEFAULT '',
    ip      TEXT DEFAULT '',
    detail  TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit_logs(ts DESC);

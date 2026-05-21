-- AI Brand Scale — Postgres schema
-- Run in Supabase SQL Editor (or via psql) ONCE to create tables.
-- Safe to re-run: uses IF NOT EXISTS.

-- ============================================================
-- USERS — replaces .tmp/users.json
-- ============================================================
CREATE TABLE IF NOT EXISTS users (
    id              TEXT PRIMARY KEY,                       -- 16-char hex (matches existing format from secrets.token_hex(8))
    email           TEXT UNIQUE NOT NULL,
    name            TEXT,
    password_hash   TEXT NOT NULL,                          -- format: "salt$hash" (PBKDF2-SHA256, 100k iters) — unchanged
    credits         INTEGER NOT NULL DEFAULT 0,             -- for future credit/billing system
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_users_email ON users (lower(email));

-- ============================================================
-- JOB HISTORY — new table, summary of every job a user runs
-- (full job state with images stays on filesystem for now)
-- ============================================================
CREATE TABLE IF NOT EXISTS job_history (
    id              TEXT PRIMARY KEY,                       -- job_id (12-char hex, matches existing code)
    user_id         TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    feature         TEXT NOT NULL,                          -- 'advertorial' | 'pixar' | 'static' | 'finance'
    title           TEXT,                                    -- short label for UI ("Brand X — Product Y")
    status          TEXT NOT NULL DEFAULT 'running',        -- 'queued' | 'running' | 'done' | 'failed' | 'cancelled'
    brief           JSONB,                                   -- input args (brand, product, prompt, etc.)
    result          JSONB,                                   -- output summary (final URLs, HTML preview path, etc.)
    error           TEXT,
    credits_cost    INTEGER NOT NULL DEFAULT 0,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_job_history_user_time
    ON job_history (user_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_job_history_user_feature
    ON job_history (user_id, feature, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_job_history_status
    ON job_history (status) WHERE status IN ('queued', 'running');

-- Auto-update updated_at on any row change
CREATE OR REPLACE FUNCTION set_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_job_history_updated_at ON job_history;
CREATE TRIGGER trg_job_history_updated_at
    BEFORE UPDATE ON job_history
    FOR EACH ROW
    EXECUTE FUNCTION set_updated_at();

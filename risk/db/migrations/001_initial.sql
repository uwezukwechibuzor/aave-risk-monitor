-- Migration 001: initial schema
-- Run with: psql $DATABASE_URL -f this_file.sql

BEGIN;

CREATE TABLE IF NOT EXISTS schema_migrations (
    version     TEXT PRIMARY KEY,
    applied_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ─── Indexer cursor ───────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS indexer_cursor (
    id          SERIAL PRIMARY KEY,
    stream_id   TEXT NOT NULL UNIQUE,
    cursor      TEXT NOT NULL,
    block_number BIGINT NOT NULL,
    block_hash  TEXT NOT NULL,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ─── Raw events ──────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS raw_events (
    id              BIGSERIAL,
    block_number    BIGINT NOT NULL,
    block_hash      TEXT NOT NULL,
    tx_hash         TEXT NOT NULL,
    log_index       INTEGER NOT NULL,
    address         TEXT NOT NULL,
    topic0          TEXT NOT NULL,
    topics          TEXT[] NOT NULL DEFAULT '{}',
    data            TEXT NOT NULL DEFAULT '',
    decoded         JSONB,
    is_final        BOOLEAN NOT NULL DEFAULT FALSE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (block_number, log_index)
) PARTITION BY RANGE (block_number);

CREATE TABLE raw_events_default PARTITION OF raw_events DEFAULT;

CREATE INDEX IF NOT EXISTS idx_raw_events_address
    ON raw_events (address, block_number);
CREATE INDEX IF NOT EXISTS idx_raw_events_topic0
    ON raw_events (topic0, block_number);
CREATE INDEX IF NOT EXISTS idx_raw_events_tx
    ON raw_events (tx_hash);

-- ─── Aave domain events ───────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS aave_supply_events (
    block_number    BIGINT NOT NULL,
    log_index       INTEGER NOT NULL,
    tx_hash         TEXT NOT NULL,
    reserve         TEXT NOT NULL,
    "user"          TEXT NOT NULL,
    on_behalf_of    TEXT NOT NULL,
    amount          NUMERIC NOT NULL,
    referral_code   INTEGER,
    is_final        BOOLEAN NOT NULL DEFAULT FALSE,
    PRIMARY KEY (block_number, log_index)
);

CREATE TABLE IF NOT EXISTS aave_withdraw_events (
    block_number    BIGINT NOT NULL,
    log_index       INTEGER NOT NULL,
    tx_hash         TEXT NOT NULL,
    reserve         TEXT NOT NULL,
    "user"          TEXT NOT NULL,
    to_address      TEXT NOT NULL,
    amount          NUMERIC NOT NULL,
    is_final        BOOLEAN NOT NULL DEFAULT FALSE,
    PRIMARY KEY (block_number, log_index)
);

CREATE TABLE IF NOT EXISTS aave_borrow_events (
    block_number    BIGINT NOT NULL,
    log_index       INTEGER NOT NULL,
    tx_hash         TEXT NOT NULL,
    reserve         TEXT NOT NULL,
    "user"          TEXT NOT NULL,
    on_behalf_of    TEXT NOT NULL,
    amount          NUMERIC NOT NULL,
    borrow_rate_mode INTEGER NOT NULL,
    borrow_rate     NUMERIC NOT NULL,
    referral_code   INTEGER,
    is_final        BOOLEAN NOT NULL DEFAULT FALSE,
    PRIMARY KEY (block_number, log_index)
);

CREATE TABLE IF NOT EXISTS aave_repay_events (
    block_number    BIGINT NOT NULL,
    log_index       INTEGER NOT NULL,
    tx_hash         TEXT NOT NULL,
    reserve         TEXT NOT NULL,
    "user"          TEXT NOT NULL,
    repayer         TEXT NOT NULL,
    amount          NUMERIC NOT NULL,
    use_atokens     BOOLEAN NOT NULL DEFAULT FALSE,
    is_final        BOOLEAN NOT NULL DEFAULT FALSE,
    PRIMARY KEY (block_number, log_index)
);

CREATE TABLE IF NOT EXISTS aave_liquidation_events (
    block_number            BIGINT NOT NULL,
    log_index               INTEGER NOT NULL,
    tx_hash                 TEXT NOT NULL,
    collateral_asset        TEXT NOT NULL,
    debt_asset              TEXT NOT NULL,
    "user"                  TEXT NOT NULL,
    debt_to_cover           NUMERIC NOT NULL,
    liquidated_collateral   NUMERIC NOT NULL,
    liquidator              TEXT NOT NULL,
    receive_atoken          BOOLEAN NOT NULL DEFAULT FALSE,
    is_final                BOOLEAN NOT NULL DEFAULT FALSE,
    PRIMARY KEY (block_number, log_index)
);

CREATE TABLE IF NOT EXISTS aave_reserve_updated_events (
    block_number            BIGINT NOT NULL,
    log_index               INTEGER NOT NULL,
    tx_hash                 TEXT NOT NULL,
    reserve                 TEXT NOT NULL,
    liquidity_rate          NUMERIC NOT NULL,
    stable_borrow_rate      NUMERIC NOT NULL,
    variable_borrow_rate    NUMERIC NOT NULL,
    liquidity_index         NUMERIC NOT NULL,
    variable_borrow_index   NUMERIC NOT NULL,
    is_final                BOOLEAN NOT NULL DEFAULT FALSE,
    PRIMARY KEY (block_number, log_index)
);

-- ─── Price / oracle events ────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS chainlink_price_events (
    block_number    BIGINT NOT NULL,
    log_index       INTEGER NOT NULL,
    tx_hash         TEXT NOT NULL,
    aggregator      TEXT NOT NULL,
    asset_symbol    TEXT,
    round_id        NUMERIC NOT NULL,
    answer          NUMERIC NOT NULL,  -- raw, divide by 10^decimals
    updated_at_ts   BIGINT NOT NULL,
    is_final        BOOLEAN NOT NULL DEFAULT FALSE,
    PRIMARY KEY (block_number, log_index)
);

CREATE INDEX IF NOT EXISTS idx_chainlink_aggregator
    ON chainlink_price_events (aggregator, block_number);

-- ─── DEX swap events ─────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS dex_swap_events (
    block_number    BIGINT NOT NULL,
    log_index       INTEGER NOT NULL,
    tx_hash         TEXT NOT NULL,
    pool_address    TEXT NOT NULL,
    protocol        TEXT NOT NULL,  -- 'uniswap_v3' | 'curve'
    sender          TEXT NOT NULL,
    recipient       TEXT NOT NULL,
    amount0         NUMERIC NOT NULL,
    amount1         NUMERIC NOT NULL,
    sqrt_price_x96  NUMERIC,        -- Uniswap v3 only
    tick            INTEGER,        -- Uniswap v3 only
    is_final        BOOLEAN NOT NULL DEFAULT FALSE,
    PRIMARY KEY (block_number, log_index)
);

CREATE INDEX IF NOT EXISTS idx_dex_swap_pool
    ON dex_swap_events (pool_address, block_number);

-- ─── Position snapshots ───────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS user_positions (
    id                      BIGSERIAL PRIMARY KEY,
    snapshot_block          BIGINT NOT NULL,
    snapshot_ts             TIMESTAMPTZ NOT NULL,
    "user"                  TEXT NOT NULL,
    total_collateral_base   NUMERIC NOT NULL,  -- USD, 8 decimals (Aave base currency)
    total_debt_base         NUMERIC NOT NULL,
    available_borrow_base   NUMERIC NOT NULL,
    current_liquidation_threshold NUMERIC NOT NULL,  -- 4-decimal (e.g. 8250 = 82.50%)
    ltv                     NUMERIC NOT NULL,
    health_factor           NUMERIC NOT NULL,  -- 18-decimal ray
    emode_category          INTEGER NOT NULL DEFAULT 0,
    UNIQUE (snapshot_block, "user")
);

CREATE INDEX IF NOT EXISTS idx_user_positions_user
    ON user_positions ("user", snapshot_block);
CREATE INDEX IF NOT EXISTS idx_user_positions_hf
    ON user_positions (health_factor, snapshot_block);

CREATE TABLE IF NOT EXISTS user_reserve_positions (
    id                      BIGSERIAL PRIMARY KEY,
    snapshot_block          BIGINT NOT NULL,
    "user"                  TEXT NOT NULL,
    reserve                 TEXT NOT NULL,
    atoken_balance          NUMERIC NOT NULL,
    variable_debt           NUMERIC NOT NULL,
    stable_debt             NUMERIC NOT NULL,
    collateral_enabled      BOOLEAN NOT NULL DEFAULT TRUE,
    UNIQUE (snapshot_block, "user", reserve)
);

-- ─── Reserve configuration snapshots ─────────────────────────────────────────
CREATE TABLE IF NOT EXISTS reserve_configs (
    snapshot_block          BIGINT NOT NULL,
    reserve                 TEXT NOT NULL,
    ltv                     NUMERIC NOT NULL,
    liquidation_threshold   NUMERIC NOT NULL,
    liquidation_bonus       NUMERIC NOT NULL,
    decimals                INTEGER NOT NULL,
    supply_cap              NUMERIC NOT NULL,
    borrow_cap              NUMERIC NOT NULL,
    emode_category          INTEGER NOT NULL DEFAULT 0,
    is_active               BOOLEAN NOT NULL DEFAULT TRUE,
    is_frozen               BOOLEAN NOT NULL DEFAULT FALSE,
    PRIMARY KEY (snapshot_block, reserve)
);

-- ─── Oracle prices ────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS oracle_prices (
    snapshot_block  BIGINT NOT NULL,
    asset           TEXT NOT NULL,
    price_usd       NUMERIC NOT NULL,  -- USD, 8 decimals
    source          TEXT NOT NULL DEFAULT 'aave_oracle',
    PRIMARY KEY (snapshot_block, asset)
);

-- ─── Signal values ────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS signal_values (
    id          BIGSERIAL PRIMARY KEY,
    signal      TEXT NOT NULL,
    asset       TEXT NOT NULL,
    scenario    TEXT NOT NULL DEFAULT '',
    block       BIGINT NOT NULL,
    ts          TIMESTAMPTZ NOT NULL DEFAULT now(),
    value       NUMERIC NOT NULL,
    threshold   NUMERIC NOT NULL,
    fired       BOOLEAN NOT NULL DEFAULT FALSE,
    metadata    JSONB
);

CREATE INDEX IF NOT EXISTS idx_signal_values_signal
    ON signal_values (signal, asset, block);

-- ─── Alerts ──────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS alerts (
    id              BIGSERIAL PRIMARY KEY,
    signal          TEXT NOT NULL,
    asset           TEXT NOT NULL,
    scenario        TEXT NOT NULL DEFAULT '',
    severity        TEXT NOT NULL,  -- 'info' | 'warning' | 'critical'
    message         TEXT NOT NULL,
    block           BIGINT NOT NULL,
    ts              TIMESTAMPTZ NOT NULL DEFAULT now(),
    resolved_at     TIMESTAMPTZ,
    sent            BOOLEAN NOT NULL DEFAULT FALSE,
    metadata        JSONB
);

CREATE INDEX IF NOT EXISTS idx_alerts_signal
    ON alerts (signal, asset, ts DESC);

-- ─── Active users registry ────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS active_users (
    address         TEXT PRIMARY KEY,
    first_seen_block BIGINT NOT NULL,
    last_seen_block  BIGINT NOT NULL,
    is_active       BOOLEAN NOT NULL DEFAULT TRUE
);

-- ─── Indexer health ───────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS indexer_health (
    id              SERIAL PRIMARY KEY,
    stream_id       TEXT NOT NULL,
    head_block      BIGINT,
    indexed_block   BIGINT,
    lag_blocks      BIGINT,
    events_per_sec  NUMERIC,
    last_checked    TIMESTAMPTZ NOT NULL DEFAULT now()
);

INSERT INTO schema_migrations (version) VALUES ('001') ON CONFLICT DO NOTHING;

COMMIT;

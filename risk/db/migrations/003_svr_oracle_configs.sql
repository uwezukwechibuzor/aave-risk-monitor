-- Migration 003: SVR oracle configuration tracking

BEGIN;

CREATE TABLE IF NOT EXISTS svr_oracle_configs (
    id          BIGSERIAL PRIMARY KEY,
    block_number BIGINT NOT NULL,
    log_index   INT    NOT NULL,
    tx_hash     TEXT   NOT NULL,
    asset       TEXT   NOT NULL,
    current_oracle TEXT NOT NULL,
    svr_oracle  TEXT   NOT NULL,
    is_final    BOOLEAN NOT NULL DEFAULT FALSE,
    indexed_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (block_number, log_index)
);

CREATE INDEX IF NOT EXISTS idx_svr_config_asset
    ON svr_oracle_configs (asset, block_number DESC);

CREATE INDEX IF NOT EXISTS idx_svr_config_block
    ON svr_oracle_configs (block_number DESC);

INSERT INTO schema_migrations (version) VALUES ('003') ON CONFLICT DO NOTHING;

COMMIT;

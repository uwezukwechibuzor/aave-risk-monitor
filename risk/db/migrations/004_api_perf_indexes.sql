-- Migration 004: indexes to make API endpoints return in <100ms

-- user_reserve_positions: snapshot_block filter used by every /markets and /wallets query.
-- Without this, every request does a full 130K-row sequential scan (~1.7s).
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_urp_snapshot
    ON user_reserve_positions (snapshot_block);

-- Covering index for /markets/summary GROUP BY reserve at a given snapshot
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_urp_snapshot_reserve
    ON user_reserve_positions (snapshot_block, reserve);

-- user_positions: snapshot_block filter used by most /wallets and /overview queries (~80ms seq scan)
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_up_snapshot
    ON user_positions (snapshot_block);

-- signal_values: DISTINCT ON (signal, asset, scenario) ORDER BY block DESC
-- Current index only covers (signal, asset, block) — missing scenario.
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_sv_lookup
    ON signal_values (signal, asset, scenario, block DESC);

-- signal_values: oracle heatmap filters on ts
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_sv_ts
    ON signal_values (ts);

-- oracle_prices: DISTINCT ON (asset) ORDER BY snapshot_block DESC used in svr/stats CTEs
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_op_asset_block
    ON oracle_prices (asset, snapshot_block DESC);

-- reserve_configs: same DISTINCT ON pattern in svr/stats
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_rc_reserve_block
    ON reserve_configs (reserve, snapshot_block DESC);

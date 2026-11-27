-- Migration 002: prune raw_events and add performance indexes

BEGIN;

-- ── 1. Truncate raw_events — all data is in typed domain tables ───────────────
TRUNCATE TABLE raw_events;

-- ── 2. Indexes for recent-activity queries (ORDER BY block_number DESC) ────────
CREATE INDEX IF NOT EXISTS idx_supply_block   ON aave_supply_events   (block_number DESC);
CREATE INDEX IF NOT EXISTS idx_withdraw_block ON aave_withdraw_events  (block_number DESC);
CREATE INDEX IF NOT EXISTS idx_borrow_block   ON aave_borrow_events    (block_number DESC);
CREATE INDEX IF NOT EXISTS idx_repay_block    ON aave_repay_events     (block_number DESC);
CREATE INDEX IF NOT EXISTS idx_liq_block      ON aave_liquidation_events (block_number DESC);
CREATE INDEX IF NOT EXISTS idx_reserve_block  ON aave_reserve_updated_events (block_number DESC);

-- ── 3. Indexes for per-asset filtering ────────────────────────────────────────
CREATE INDEX IF NOT EXISTS idx_supply_reserve   ON aave_supply_events   (reserve, block_number DESC);
CREATE INDEX IF NOT EXISTS idx_withdraw_reserve ON aave_withdraw_events  (reserve, block_number DESC);
CREATE INDEX IF NOT EXISTS idx_borrow_reserve   ON aave_borrow_events    (reserve, block_number DESC);
CREATE INDEX IF NOT EXISTS idx_repay_reserve    ON aave_repay_events     (reserve, block_number DESC);
CREATE INDEX IF NOT EXISTS idx_reserve_updated_reserve ON aave_reserve_updated_events (reserve, block_number DESC);

-- ── 4. Indexes for liquidation queries ────────────────────────────────────────
CREATE INDEX IF NOT EXISTS idx_liq_collateral ON aave_liquidation_events (collateral_asset, block_number DESC);
CREATE INDEX IF NOT EXISTS idx_liq_debt       ON aave_liquidation_events (debt_asset, block_number DESC);
CREATE INDEX IF NOT EXISTS idx_liq_user       ON aave_liquidation_events ("user", block_number DESC);

-- ── 5. Indexes for user/wallet queries ────────────────────────────────────────
CREATE INDEX IF NOT EXISTS idx_up_debt_desc   ON user_positions (snapshot_block, total_debt_base DESC)
    WHERE total_debt_base > 0;
CREATE INDEX IF NOT EXISTS idx_up_coll_desc   ON user_positions (snapshot_block, total_collateral_base DESC);
CREATE INDEX IF NOT EXISTS idx_up_hf          ON user_positions (snapshot_block, health_factor)
    WHERE total_debt_base > 0;
CREATE INDEX IF NOT EXISTS idx_up_emode       ON user_positions (snapshot_block, emode_category);

-- ── 6. Partial index for 24h liquidation count ────────────────────────────────
-- (WHERE block_number >= head - 7200 — covered by idx_liq_block above)

-- ── 7. Optimize active_users lookups ─────────────────────────────────────────
CREATE INDEX IF NOT EXISTS idx_active_users_last ON active_users (last_seen_block DESC);

-- ── 8. DEX swap pool+block for coverage ratio ─────────────────────────────────
-- idx_dex_swap_pool already exists (pool_address, block_number)

INSERT INTO schema_migrations (version) VALUES ('002') ON CONFLICT DO NOTHING;

COMMIT;

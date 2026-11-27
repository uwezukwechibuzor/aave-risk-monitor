-- Migration 005: prevent duplicate alerts per (signal, asset, scenario, block)
--
-- Without this, re-running signals at the same block after the 6-hour cooldown
-- expires creates a second alert for every signal even though nothing changed.

CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS idx_alerts_key
    ON alerts (signal, asset, scenario, block);

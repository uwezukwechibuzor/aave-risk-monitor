"""FastAPI application entrypoint."""

from __future__ import annotations

import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from prometheus_client import CONTENT_TYPE_LATEST, CollectorRegistry, generate_latest

from risk.api.models import (
    AlertItem,
    AlertsResponse,
    AssetInfo,
    AssetsResponse,
    BacktestResponse,
    BacktestSummary,
    HealthResponse,
    HeatmapCell,
    HeatmapResponse,
    ShockResult,
    SignalsResponse,
    SignalValue,
    StressResponse,
)
from risk.config import get_settings, load_assets_config
from risk.db import close_pool, get_pool, init_pool
from risk.rpc import EthRPC

logger = logging.getLogger(__name__)
settings = get_settings()

STATIC_DIR = Path(__file__).parent / "static"
RAY = 1e27
SECONDS_PER_YEAR = 31_536_000


def _sym_map(assets_cfg: dict[str, Any]) -> dict[str, str]:
    """lowercase address → symbol"""
    return {v["address"].lower(): k for k, v in assets_cfg.get("assets", {}).items()}


def _fmt(v: Any) -> float:
    try:
        return float(v or 0)
    except Exception:
        return 0.0


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    await init_pool(settings.database_url)
    app.state.rpc = EthRPC(
        url=settings.graphops_rpc_url,
        api_key=settings.graphops_api_key,
        max_calls=settings.max_rpc_calls_per_run,
    )
    logger.info("API startup complete.")
    yield
    await close_pool()
    await app.state.rpc.close()


app = FastAPI(
    title="Aave Collateral Stress Monitor",
    description="Risk monitoring for Aave v3 Ethereum collateral positions",
    version="0.1.0",
    lifespan=lifespan,
)

if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/", response_class=HTMLResponse, include_in_schema=False)
async def dashboard() -> HTMLResponse:
    dashboard_path = STATIC_DIR / "dashboard.html"
    if dashboard_path.exists():
        return HTMLResponse(content=dashboard_path.read_text())
    return HTMLResponse(content="<h1>Dashboard not found</h1>", status_code=404)


# ── Health ────────────────────────────────────────────────────────────────────

@app.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    db_ok = False
    lag: int | None = None
    try:
        pool = get_pool()
        async with pool.acquire() as conn:
            await conn.execute("SELECT 1")
            row = await conn.fetchrow(
                "SELECT lag_blocks FROM indexer_health ORDER BY last_checked DESC LIMIT 1"
            )
            if row:
                lag = row["lag_blocks"]
        db_ok = True
    except Exception as e:
        logger.warning("Health DB check failed: %s", e)

    rpc_ok = False
    try:
        rpc: EthRPC = app.state.rpc
        await rpc.eth_block_number()
        rpc_ok = True
    except Exception as e:
        logger.warning("Health RPC check failed: %s", e)

    return HealthResponse(
        status="ok" if (db_ok and rpc_ok) else "degraded",
        database="ok" if db_ok else "error",
        indexer_lag_blocks=lag,
        rpc_ok=rpc_ok,
        timestamp=datetime.now(tz=UTC),
    )


@app.get("/metrics", response_class=PlainTextResponse)
async def metrics() -> PlainTextResponse:
    registry = CollectorRegistry(auto_describe=True)
    output = generate_latest(registry)
    return PlainTextResponse(content=output.decode(), media_type=CONTENT_TYPE_LATEST)


# ── Overview ──────────────────────────────────────────────────────────────────

@app.get("/overview/stats")
async def overview_stats() -> dict[str, Any]:
    pool = get_pool()
    async with pool.acquire() as conn:
        pos = await conn.fetchrow("""
            SELECT
                SUM(total_collateral_base)/1e8   AS total_supply_usd,
                SUM(total_debt_base)/1e8          AS total_borrow_usd,
                (SUM(total_collateral_base) - SUM(total_debt_base))/1e8 AS tvl_usd,
                COUNT(*) FILTER (WHERE total_debt_base > 0) AS active_positions
            FROM user_positions
            WHERE snapshot_block = (SELECT MAX(snapshot_block) FROM user_positions)
        """)
        users = await conn.fetchval("SELECT COUNT(*) FROM active_users")
        evts = await conn.fetchrow("""
            SELECT
                (SELECT COUNT(*) FROM aave_supply_events)       AS supply,
                (SELECT COUNT(*) FROM aave_borrow_events)       AS borrow,
                (SELECT COUNT(*) FROM aave_repay_events)        AS repay,
                (SELECT COUNT(*) FROM aave_withdraw_events)     AS withdraw,
                (SELECT COUNT(*) FROM aave_liquidation_events)  AS liquidation,
                (SELECT COUNT(*) FROM dex_swap_events)          AS dex_swaps
        """)
        head = await conn.fetchval(
            "SELECT MAX(block_number) FROM indexer_cursor"
        ) or 26_500_000
        liq24 = await conn.fetchval(
            "SELECT COUNT(*) FROM aave_liquidation_events WHERE block_number >= $1",
            head - 7200,
        )
    return {
        "total_supply_usd":    _fmt(pos["total_supply_usd"]),
        "total_borrow_usd":    _fmt(pos["total_borrow_usd"]),
        "tvl_usd":             _fmt(pos["tvl_usd"]),
        "active_positions":    int(pos["active_positions"] or 0),
        "total_users":         int(users or 0),
        "supply_count":        int(evts["supply"] or 0),
        "borrow_count":        int(evts["borrow"] or 0),
        "repay_count":         int(evts["repay"] or 0),
        "withdraw_count":      int(evts["withdraw"] or 0),
        "liquidation_count":   int(evts["liquidation"] or 0),
        "dex_swap_count":      int(evts["dex_swaps"] or 0),
        "liquidation_count_24h": int(liq24 or 0),
    }


@app.get("/overview/activity")
async def overview_activity(days: int = Query(90, ge=7, le=90)) -> dict[str, Any]:
    pool = get_pool()
    async with pool.acquire() as conn:
        ref = await conn.fetchrow(
            "SELECT MAX(snapshot_block) AS hb, MAX(snapshot_ts) AS ht FROM user_positions"
        )
        head_block = int(ref["hb"] or 26_500_000)
        head_ts = ref["ht"] or datetime.now(UTC)
        min_block = head_block - days * 7200

        rows = await conn.fetch("""
            WITH ref(hb, ht) AS (VALUES ($1::bigint, $2::timestamptz)),
            combined AS (
                SELECT block_number, 'supply'      AS etype FROM aave_supply_events      WHERE block_number >= $3
                UNION ALL
                SELECT block_number, 'borrow'                FROM aave_borrow_events      WHERE block_number >= $3
                UNION ALL
                SELECT block_number, 'repay'                 FROM aave_repay_events       WHERE block_number >= $3
                UNION ALL
                SELECT block_number, 'withdraw'              FROM aave_withdraw_events    WHERE block_number >= $3
                UNION ALL
                SELECT block_number, 'liquidation'           FROM aave_liquidation_events WHERE block_number >= $3
            )
            SELECT
                date_trunc('day', ht - ((hb - block_number) * interval '12 seconds')) AS day,
                etype,
                COUNT(*) AS cnt
            FROM combined, ref
            GROUP BY day, etype
            ORDER BY day
        """, head_block, head_ts, min_block)

    day_map: dict[str, dict[str, Any]] = {}
    for r in rows:
        d = str(r["day"].date())
        if d not in day_map:
            day_map[d] = {"day": d, "supply": 0, "borrow": 0, "repay": 0, "withdraw": 0, "liquidation": 0}
        day_map[d][r["etype"]] = int(r["cnt"])

    return {"days": sorted(day_map.values(), key=lambda x: x["day"])}


# ── Markets ───────────────────────────────────────────────────────────────────

@app.get("/markets/summary")
async def markets_summary() -> dict[str, Any]:
    pool = get_pool()
    assets_cfg = load_assets_config()
    sym_map = _sym_map(assets_cfg)

    async with pool.acquire() as conn:
        snap_block = await conn.fetchval(
            "SELECT MAX(snapshot_block) FROM user_reserve_positions"
        )
        pos_rows = await conn.fetch("""
            SELECT reserve,
                   SUM(atoken_balance)                AS supply_tokens,
                   SUM(variable_debt + stable_debt)   AS borrow_tokens
            FROM user_reserve_positions
            WHERE snapshot_block = $1
            GROUP BY reserve
        """, snap_block)

        price_rows = await conn.fetch(
            "SELECT asset, price_usd FROM oracle_prices WHERE snapshot_block = $1", snap_block
        )
        cfg_rows = await conn.fetch(
            """SELECT reserve, decimals, ltv, liquidation_threshold, liquidation_bonus,
                      supply_cap, borrow_cap
               FROM reserve_configs WHERE snapshot_block = $1""", snap_block
        )
        rate_rows = await conn.fetch("""
            SELECT DISTINCT ON (reserve)
                reserve, liquidity_rate, variable_borrow_rate
            FROM aave_reserve_updated_events
            ORDER BY reserve, block_number DESC
        """)
        util_rows = await conn.fetch("""
            SELECT DISTINCT ON (asset) asset, value AS utilization
            FROM signal_values
            WHERE signal = 'utilization' AND scenario = 'utilization'
            ORDER BY asset, block DESC
        """)

    prices  = {r["asset"].lower():   _fmt(r["price_usd"]) / 1e8 for r in price_rows}
    cfgs    = {r["reserve"].lower(): r                           for r in cfg_rows}
    rates   = {r["reserve"].lower(): r                           for r in rate_rows}
    utils   = {r["asset"]:           _fmt(r["utilization"])      for r in util_rows}

    assets = []
    for row in pos_rows:
        addr = row["reserve"].lower()
        cfg  = cfgs.get(addr, {})
        dec  = int(cfg.get("decimals", 18) if cfg else 18)
        price = prices.get(addr, 0.0)
        div   = 10 ** dec

        supply_tokens = _fmt(row["supply_tokens"]) / div
        borrow_tokens = _fmt(row["borrow_tokens"]) / div
        supply_usd    = supply_tokens * price
        borrow_usd    = borrow_tokens * price

        rate = rates.get(addr, {})
        supply_apy = _fmt(rate.get("liquidity_rate"))       / RAY * 100 if rate else 0.0
        borrow_apy = _fmt(rate.get("variable_borrow_rate")) / RAY * 100 if rate else 0.0

        symbol = sym_map.get(addr, addr[:8] + "…")
        utilization = utils.get(symbol, borrow_usd / supply_usd if supply_usd > 0 else 0.0)

        lt  = _fmt(cfg.get("liquidation_threshold")) if cfg else 0.0
        ltv = _fmt(cfg.get("ltv"))                   if cfg else 0.0
        lb  = _fmt(cfg.get("liquidation_bonus"))      if cfg else 10500.0

        assets.append({
            "symbol":               symbol,
            "address":              row["reserve"],
            "supply_usd":           supply_usd,
            "borrow_usd":           borrow_usd,
            "utilization":          utilization,
            "supply_apy":           round(supply_apy, 4),
            "borrow_apy":           round(borrow_apy, 4),
            "ltv":                  round(ltv / 100, 2),
            "liquidation_threshold": round(lt / 100, 2),
            "liquidation_bonus":    round((lb - 10000) / 100, 2),
            "price_usd":            price,
            "supply_cap":           _fmt(cfg.get("supply_cap")) if cfg else 0.0,
            "borrow_cap":           _fmt(cfg.get("borrow_cap")) if cfg else 0.0,
        })

    assets.sort(key=lambda x: x["supply_usd"], reverse=True)
    return {"assets": assets, "snapshot_block": snap_block}


# ── Liquidations ──────────────────────────────────────────────────────────────

@app.get("/liquidations/stats")
async def liquidations_stats() -> dict[str, Any]:
    pool = get_pool()
    assets_cfg = load_assets_config()
    sym_map = _sym_map(assets_cfg)

    async with pool.acquire() as conn:
        snap_block = await conn.fetchval("SELECT MAX(snapshot_block) FROM oracle_prices") or 0
        price_rows = await conn.fetch(
            "SELECT asset, price_usd FROM oracle_prices WHERE snapshot_block = $1", snap_block
        )
        cfg_rows = await conn.fetch(
            "SELECT reserve, decimals, liquidation_bonus FROM reserve_configs WHERE snapshot_block = $1", snap_block
        )
        rows = await conn.fetch("""
            SELECT collateral_asset, debt_asset, COUNT(*) AS cnt,
                   SUM(debt_to_cover) AS total_debt, SUM(liquidated_collateral) AS total_coll
            FROM aave_liquidation_events
            GROUP BY collateral_asset, debt_asset
        """)
        total_count = await conn.fetchval("SELECT COUNT(*) FROM aave_liquidation_events") or 0

    prices = {r["asset"].lower(): _fmt(r["price_usd"]) / 1e8 for r in price_rows}
    cfgs   = {r["reserve"].lower(): r for r in cfg_rows}

    total_volume_usd = 0.0
    total_bonus_usd  = 0.0
    for row in rows:
        debt_addr = row["debt_asset"].lower()
        coll_addr = row["collateral_asset"].lower()
        d_dec = int(cfgs[debt_addr]["decimals"]) if debt_addr in cfgs else 18
        c_dec = int(cfgs[coll_addr]["decimals"]) if coll_addr in cfgs else 18
        d_price = prices.get(debt_addr, 0.0)
        c_price = prices.get(coll_addr, 0.0)
        lb = _fmt(cfgs[coll_addr].get("liquidation_bonus")) if coll_addr in cfgs else 10500.0

        vol  = _fmt(row["total_debt"]) / (10**d_dec) * d_price
        coll = _fmt(row["total_coll"]) / (10**c_dec) * c_price
        bonus = coll - vol
        total_volume_usd += vol
        total_bonus_usd  += max(bonus, 0)

    return {
        "total_count":       int(total_count),
        "total_volume_usd":  round(total_volume_usd, 2),
        "total_bonus_usd":   round(total_bonus_usd, 2),
    }


@app.get("/liquidations/history")
async def liquidations_history(days: int = Query(90, ge=7, le=90)) -> dict[str, Any]:
    pool = get_pool()
    assets_cfg = load_assets_config()
    sym_map = _sym_map(assets_cfg)

    async with pool.acquire() as conn:
        ref = await conn.fetchrow(
            "SELECT MAX(snapshot_block) AS hb, MAX(snapshot_ts) AS ht FROM user_positions"
        )
        head_block = int(ref["hb"] or 26_500_000)
        head_ts    = ref["ht"] or datetime.now(UTC)
        min_block  = head_block - days * 7200

        rows = await conn.fetch("""
            WITH ref(hb, ht) AS (VALUES ($1::bigint, $2::timestamptz))
            SELECT
                date_trunc('day', ht - ((hb - l.block_number) * interval '12 seconds')) AS day,
                l.collateral_asset,
                COUNT(*)               AS cnt,
                SUM(l.debt_to_cover)   AS total_debt
            FROM aave_liquidation_events l, ref
            WHERE l.block_number >= $3
            GROUP BY day, l.collateral_asset
            ORDER BY day
        """, head_block, head_ts, min_block)

    day_map: dict[str, dict[str, Any]] = {}
    assets_seen: set[str] = set()
    for r in rows:
        d    = str(r["day"].date())
        sym  = sym_map.get(r["collateral_asset"].lower(), r["collateral_asset"][:8])
        assets_seen.add(sym)
        if d not in day_map:
            day_map[d] = {"day": d}
        day_map[d][sym] = day_map[d].get(sym, 0) + int(r["cnt"])

    return {
        "days":   sorted(day_map.values(), key=lambda x: x["day"]),
        "assets": sorted(assets_seen),
    }


@app.get("/liquidations/recent")
async def liquidations_recent(limit: int = Query(50, ge=1, le=200)) -> dict[str, Any]:
    pool = get_pool()
    assets_cfg = load_assets_config()
    sym_map = _sym_map(assets_cfg)

    async with pool.acquire() as conn:
        ref = await conn.fetchrow(
            "SELECT MAX(snapshot_block) AS hb, MAX(snapshot_ts) AS ht FROM user_positions"
        )
        head_block = int(ref["hb"] or 26_500_000)
        head_ts    = ref["ht"] or datetime.now(UTC)

        rows = await conn.fetch("""
            WITH ref(hb, ht) AS (VALUES ($1::bigint, $2::timestamptz))
            SELECT
                l.block_number, l.tx_hash, l.collateral_asset, l.debt_asset,
                l."user", l.debt_to_cover, l.liquidated_collateral,
                l.liquidator, l.receive_atoken,
                (ht - ((hb - l.block_number) * interval '12 seconds')) AS approx_ts
            FROM aave_liquidation_events l, ref
            ORDER BY l.block_number DESC
            LIMIT $3
        """, head_block, head_ts, limit)

    items = []
    for r in rows:
        items.append({
            "block_number":        int(r["block_number"]),
            "tx_hash":             r["tx_hash"],
            "collateral_asset":    sym_map.get(r["collateral_asset"].lower(), r["collateral_asset"][:10]),
            "debt_asset":          sym_map.get(r["debt_asset"].lower(), r["debt_asset"][:10]),
            "user":                r["user"],
            "debt_to_cover":       _fmt(r["debt_to_cover"]),
            "liquidated_collateral": _fmt(r["liquidated_collateral"]),
            "liquidator":          r["liquidator"],
            "timestamp":           r["approx_ts"].isoformat() if r["approx_ts"] else "",
        })
    return {"liquidations": items}


# ── Wallets ───────────────────────────────────────────────────────────────────

@app.get("/wallets/top-borrowers")
async def top_borrowers(limit: int = Query(20, ge=5, le=100)) -> dict[str, Any]:
    pool = get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT "user", total_collateral_base/1e8 AS supply_usd,
                   total_debt_base/1e8 AS borrow_usd,
                   health_factor, emode_category, ltv
            FROM user_positions
            WHERE snapshot_block = (SELECT MAX(snapshot_block) FROM user_positions)
              AND total_debt_base > 0 AND health_factor < 999
            ORDER BY total_debt_base DESC
            LIMIT $1
        """, limit)
    return {
        "wallets": [
            {
                "user":        r["user"],
                "supply_usd":  _fmt(r["supply_usd"]),
                "borrow_usd":  _fmt(r["borrow_usd"]),
                "health_factor": _fmt(r["health_factor"]),
                "emode":       int(r["emode_category"]),
                "ltv":         _fmt(r["ltv"]) / 100,
            }
            for r in rows
        ]
    }


@app.get("/wallets/top-suppliers")
async def top_suppliers(limit: int = Query(20, ge=5, le=100)) -> dict[str, Any]:
    pool = get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT "user", total_collateral_base/1e8 AS supply_usd,
                   total_debt_base/1e8 AS borrow_usd,
                   health_factor, emode_category
            FROM user_positions
            WHERE snapshot_block = (SELECT MAX(snapshot_block) FROM user_positions)
              AND total_collateral_base > 0 AND health_factor < 999
            ORDER BY total_collateral_base DESC
            LIMIT $1
        """, limit)
    return {
        "wallets": [
            {
                "user":        r["user"],
                "supply_usd":  _fmt(r["supply_usd"]),
                "borrow_usd":  _fmt(r["borrow_usd"]),
                "health_factor": _fmt(r["health_factor"]),
                "emode":       int(r["emode_category"]),
            }
            for r in rows
        ]
    }


@app.get("/wallets/hf-distribution")
async def hf_distribution() -> dict[str, Any]:
    pool = get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT
                CASE
                    WHEN health_factor < 1.0  THEN '<1.0 (Liquidatable)'
                    WHEN health_factor < 1.05 THEN '1.0–1.05 (Danger)'
                    WHEN health_factor < 1.1  THEN '1.05–1.1 (High Risk)'
                    WHEN health_factor < 1.25 THEN '1.1–1.25 (Elevated)'
                    WHEN health_factor < 1.5  THEN '1.25–1.5 (Moderate)'
                    WHEN health_factor < 2.0  THEN '1.5–2.0 (Healthy)'
                    ELSE '>2.0 (Safe)'
                END AS band,
                COUNT(*) AS count,
                SUM(total_debt_base)/1e8 AS debt_usd
            FROM user_positions
            WHERE snapshot_block = (SELECT MAX(snapshot_block) FROM user_positions)
              AND total_debt_base > 0 AND health_factor < 999
            GROUP BY band
            ORDER BY MIN(health_factor)
        """)
    return {
        "bands": [
            {"band": r["band"], "count": int(r["count"]), "debt_usd": _fmt(r["debt_usd"])}
            for r in rows
        ]
    }


@app.get("/wallets/emode-distribution")
async def emode_distribution() -> dict[str, Any]:
    pool = get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT emode_category, COUNT(*) AS count,
                   SUM(total_collateral_base)/1e8 AS supply_usd,
                   SUM(total_debt_base)/1e8 AS borrow_usd
            FROM user_positions
            WHERE snapshot_block = (SELECT MAX(snapshot_block) FROM user_positions)
              AND health_factor < 999
            GROUP BY emode_category
            ORDER BY emode_category
        """)
    labels = {0: "No E-Mode", 1: "ETH Correlated", 2: "Stablecoins"}
    return {
        "emodes": [
            {
                "category": int(r["emode_category"]),
                "label":    labels.get(int(r["emode_category"]), f"E-Mode {r['emode_category']}"),
                "count":    int(r["count"]),
                "supply_usd": _fmt(r["supply_usd"]),
                "borrow_usd": _fmt(r["borrow_usd"]),
            }
            for r in rows
        ]
    }


# ── Activity feed ─────────────────────────────────────────────────────────────

@app.get("/activity/recent")
async def recent_activity(limit: int = Query(50, ge=10, le=200)) -> dict[str, Any]:
    pool = get_pool()
    assets_cfg = load_assets_config()
    sym_map = _sym_map(assets_cfg)

    async with pool.acquire() as conn:
        ref = await conn.fetchrow(
            "SELECT MAX(snapshot_block) AS hb, MAX(snapshot_ts) AS ht FROM user_positions"
        )
        head_block = int(ref["hb"] or 26_500_000)
        head_ts    = ref["ht"] or datetime.now(UTC)

        rows = await conn.fetch("""
            WITH ref(hb, ht) AS (VALUES ($1::bigint, $2::timestamptz)),
            combined AS (
                SELECT block_number, tx_hash, reserve, "user", amount, 'Supply' AS etype
                FROM aave_supply_events
                UNION ALL
                SELECT block_number, tx_hash, reserve, "user", amount, 'Borrow'
                FROM aave_borrow_events
                UNION ALL
                SELECT block_number, tx_hash, reserve, "user", amount, 'Repay'
                FROM aave_repay_events
                UNION ALL
                SELECT block_number, tx_hash, reserve, "user", amount, 'Withdraw'
                FROM aave_withdraw_events
            )
            SELECT
                c.block_number, c.tx_hash, c.reserve, c."user", c.amount, c.etype,
                (ht - ((hb - c.block_number) * interval '12 seconds')) AS approx_ts
            FROM combined c, ref
            ORDER BY c.block_number DESC
            LIMIT $3
        """, head_block, head_ts, limit)

    return {
        "events": [
            {
                "block_number": int(r["block_number"]),
                "tx_hash":      r["tx_hash"],
                "event_type":   r["etype"],
                "reserve":      sym_map.get(r["reserve"].lower(), r["reserve"][:10]),
                "user":         r["user"],
                "amount":       _fmt(r["amount"]),
                "timestamp":    r["approx_ts"].isoformat() if r["approx_ts"] else "",
            }
            for r in rows
        ]
    }


# ── Existing endpoints (unchanged) ────────────────────────────────────────────

@app.get("/assets", response_model=AssetsResponse)
async def list_assets() -> AssetsResponse:
    assets_cfg = load_assets_config()
    items = [
        AssetInfo(symbol=sym, address=cfg["address"], decimals=cfg.get("decimals", 18))
        for sym, cfg in assets_cfg.get("assets", {}).items()
    ]
    return AssetsResponse(assets=items)


@app.get("/stress", response_model=StressResponse)
async def stress(
    asset: str = Query(..., description="Asset symbol, e.g. wstETH"),
    scenario: str = Query("asset_depeg", description="asset_depeg or market_wide"),
) -> StressResponse:
    pool = get_pool()
    async with pool.acquire() as conn:
        block_row = await conn.fetchrow("SELECT MAX(snapshot_block) AS block FROM user_positions")
        if not block_row or not block_row["block"]:
            raise HTTPException(status_code=404, detail="No snapshot data available")
        block = int(block_row["block"])

        rows = await conn.fetch(
            """
            SELECT scenario, value, metadata
            FROM signal_values
            WHERE signal = 'liquidation_at_risk' AND asset = $1
              AND scenario LIKE $2 AND block = $3
            ORDER BY scenario
            """,
            asset, f"{scenario}:%", block,
        )

    shocks: list[ShockResult] = []
    for row in rows:
        sc = str(row["scenario"])
        shock_key = sc.split(":", 1)[1] if ":" in sc else sc
        value = Decimal(str(row["value"]))
        async with pool.acquire() as conn:
            cov_rows = await conn.fetch(
                "SELECT scenario, value FROM signal_values "
                "WHERE signal='coverage_ratio' AND asset=$1 AND block=$2",
                asset, block,
            )
        cov_map = {r["scenario"]: Decimal(str(r["value"])) for r in cov_rows}
        shocks.append(ShockResult(
            shock=shock_key,
            liquidatable_debt_usd=value,
            coverage_ratio_2pct=cov_map.get("2pct"),
            coverage_ratio_5pct=cov_map.get("5pct"),
        ))

    return StressResponse(asset=asset, scenario=scenario, block=block, shocks=shocks)


@app.get("/heatmap/liquidation", response_model=HeatmapResponse)
async def heatmap_liquidation(
    scenario: str = Query("asset_depeg"),
) -> HeatmapResponse:
    pool = get_pool()
    assets_cfg = load_assets_config()
    asset_symbols = list(assets_cfg.get("assets", {}).keys())
    shocks = ["-0.05", "-0.1", "-0.2", "-0.3"]

    async with pool.acquire() as conn:
        block_row = await conn.fetchrow("SELECT MAX(snapshot_block) AS b FROM user_positions")
        block = block_row["b"] if block_row and block_row["b"] else 0
        rows = await conn.fetch(
            """
            SELECT asset, scenario, value FROM signal_values
            WHERE signal = 'liquidation_at_risk' AND block = $1 AND scenario LIKE $2
            """,
            block, f"{scenario}:%",
        )

    cell_map: dict[tuple[str, str], Decimal] = {}
    for row in rows:
        sc    = str(row["scenario"])
        shock = sc.split(":", 1)[1] if ":" in sc else sc
        cell_map[(str(row["asset"]), shock)] = Decimal(str(row["value"]))

    cells = [
        HeatmapCell(row=asset, col=shock, value=cell_map.get((asset, shock), Decimal("0")))
        for asset in asset_symbols for shock in shocks
    ]
    return HeatmapResponse(rows=asset_symbols, cols=shocks, cells=cells)


@app.get("/heatmap/health-factor", response_model=HeatmapResponse)
async def heatmap_health_factor(days: int = Query(30, ge=1, le=365)) -> HeatmapResponse:
    from datetime import timedelta
    pool = get_pool()
    cutoff = datetime.now(tz=UTC) - timedelta(days=days)
    bands  = ["<1.0", "1.0-1.05", "1.05-1.1", "1.1-1.25", "1.25-1.5", ">1.5"]

    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT
                date_trunc('day', snapshot_ts) AS day,
                CASE
                    WHEN health_factor < 1.0  THEN '<1.0'
                    WHEN health_factor < 1.05 THEN '1.0-1.05'
                    WHEN health_factor < 1.1  THEN '1.05-1.1'
                    WHEN health_factor < 1.25 THEN '1.1-1.25'
                    WHEN health_factor < 1.5  THEN '1.25-1.5'
                    ELSE '>1.5'
                END AS band,
                SUM(total_debt_base)/1e8 AS debt_usd
            FROM user_positions
            WHERE snapshot_ts >= $1 AND total_debt_base > 0
            GROUP BY day, band ORDER BY day
            """,
            cutoff,
        )

    dates = sorted(set(str(r["day"].date()) for r in rows))
    cells = [
        HeatmapCell(row=str(r["band"]), col=str(r["day"].date()), value=Decimal(str(r["debt_usd"])))
        for r in rows
    ]
    return HeatmapResponse(rows=bands, cols=dates, cells=cells)


@app.get("/heatmap/oracle-deviation", response_model=HeatmapResponse)
async def heatmap_oracle_deviation(days: int = Query(30, ge=1, le=365)) -> HeatmapResponse:
    from datetime import timedelta
    pool = get_pool()
    cutoff = datetime.now(tz=UTC) - timedelta(days=days)
    assets_cfg = load_assets_config()
    asset_symbols = list(assets_cfg.get("assets", {}).keys())
    hours = [str(h) for h in range(24)]

    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT asset, EXTRACT(HOUR FROM ts)::int AS hour,
                   PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY value) AS median_bps
            FROM signal_values
            WHERE signal = 'oracle_deviation' AND scenario LIKE 'spot:%' AND ts >= $1
            GROUP BY asset, hour ORDER BY asset, hour
            """,
            cutoff,
        )

    cells = [
        HeatmapCell(row=str(r["asset"]), col=str(r["hour"]), value=Decimal(str(r["median_bps"])))
        for r in rows
    ]
    return HeatmapResponse(rows=asset_symbols, cols=hours, cells=cells)


@app.get("/signals/latest", response_model=SignalsResponse)
async def signals_latest() -> SignalsResponse:
    pool = get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT DISTINCT ON (signal, asset, scenario)
                signal, asset, scenario, block, value, threshold, fired, ts, metadata
            FROM signal_values
            ORDER BY signal, asset, scenario, block DESC
            """
        )
    signals = [
        SignalValue(
            signal=r["signal"], asset=r["asset"], scenario=r["scenario"],
            block=r["block"], value=Decimal(str(r["value"])),
            threshold=Decimal(str(r["threshold"])), fired=bool(r["fired"]),
            ts=r["ts"], metadata=r["metadata"],
        )
        for r in rows
    ]
    return SignalsResponse(signals=signals)


@app.get("/alerts", response_model=AlertsResponse)
async def alerts(since: datetime | None = None) -> AlertsResponse:
    pool = get_pool()
    async with pool.acquire() as conn:
        if since:
            rows = await conn.fetch(
                "SELECT * FROM alerts WHERE ts >= $1 ORDER BY ts DESC LIMIT 200", since
            )
        else:
            rows = await conn.fetch("SELECT * FROM alerts ORDER BY ts DESC LIMIT 200")
    items = [
        AlertItem(
            id=r["id"], signal=r["signal"], asset=r["asset"], scenario=r["scenario"],
            severity=r["severity"], message=r["message"], block=r["block"],
            ts=r["ts"], resolved_at=r["resolved_at"],
        )
        for r in rows
    ]
    return AlertsResponse(alerts=items)


@app.get("/backtest/summary", response_model=BacktestResponse)
async def backtest_summary() -> BacktestResponse:
    pool = get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT signal, asset, scenario,
                   COUNT(*) AS total,
                   SUM(CASE WHEN fired THEN 1 ELSE 0 END) AS fires,
                   PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY value) AS p50,
                   PERCENTILE_CONT(0.95) WITHIN GROUP (ORDER BY value) AS p95
            FROM signal_values
            GROUP BY signal, asset, scenario
            ORDER BY signal, asset, scenario
            """
        )
    summaries = []
    for r in rows:
        total = int(r["total"])
        fires = int(r["fires"])
        fire_rate = Decimal(fires) / Decimal(total) if total > 0 else Decimal("0")
        p50 = Decimal(str(r["p50"])) if r["p50"] is not None else Decimal("0")
        p95 = Decimal(str(r["p95"])) if r["p95"] is not None else Decimal("0")
        summaries.append(BacktestSummary(
            signal=r["signal"], asset=r["asset"], scenario=r["scenario"],
            fire_count=fires, total_count=total, fire_rate=fire_rate,
            max_streak=0, p50=p50, p95=p95, recommended_threshold=p95,
        ))
    return BacktestResponse(summaries=summaries)


# ── SVR (Smart Value Recapture) ───────────────────────────────────────────────
#
# How SVR works on Aave v3 Ethereum Core:
#   Chainlink SVR uses a DualAggregator (0x7c7FdFCa295a787ded12Bb5c1A49A8D2cC20E3F8)
#   that holds two prices: a "slow" price for liquidations and a "fast" price for HF.
#   The WETH EACAggregatorProxy (0x5424384B...) was upgraded to this DualAggregator
#   on 2025-03-18 (block 22,076,436). Both WstETHPriceCapAdapter and WeETHPriceCapAdapter
#   reference this same WETH proxy as their BASE_TO_USD_AGGREGATOR, so wstETH and weETH
#   liquidations on Core are SVR liquidations from that block onward.
#
#   The SvrOracleSteward (0x8b493f416...) is a governance layer for future oracle changes
#   and emits SvrOracleConfigChanged events — monitored by our indexer.

# Chainlink SVR DualAggregator went live at this block (EACAggregatorProxy upgraded)
# Lido SvrOracleSteward then activated SVR at block 22,467,327 (block 22,467,327 ~ 2025-05-11)
# Core market wstETH/weETH use the same DualAggregator via PriceCapAdapter since block 22,076,436
_SVR_ACTIVATION_BLOCK = 22_076_436  # 2025-03-18, DualAggregator live

# Assets on the Core market confirmed to use the SVR DualAggregator (via their PriceCapAdapter)
# Verification: BASE_TO_USD_AGGREGATOR = 0x5424384B (WETH proxy) → 0x7c7FdFC (DualAggregator)
_SVR_ASSETS_CORE = {
    "wstETH": "0x7f39C581F595B53c5cb19bD0b3f8dA6c935E2Ca0",
    "weETH":  "0xCd5fE23C85820F7B72D0926FC9b05b43E359b7ee",
}

_SVR_DUAL_AGGREGATOR = "0x7c7FdFCa295a787ded12Bb5c1A49A8D2cC20E3F8"
_WETH_PROXY = "0x5424384B256154046E9667dDFaaa5e550145215e"

@app.get("/svr/stats")
async def svr_stats() -> dict[str, Any]:
    import datetime as dt

    pool = get_pool()
    sym_map = _sym_map(load_assets_config())
    async with pool.acquire() as conn:
        head = await conn.fetchval("SELECT MAX(snapshot_block) FROM oracle_prices") or 0
        head_block = await conn.fetchval("SELECT MAX(block_number) FROM indexer_cursor") or 0

        # SVR assets on Core market: verified on-chain via oracle chain
        #   asset oracle → PriceCapAdapter → BASE_TO_USD_AGGREGATOR = WETH proxy (0x5424384B)
        #   WETH proxy → DualAggregator (0x7c7FdFC) — activated 2025-03-18, block 22,076,436
        # We seed active_svr with the confirmed static list; future changes via SvrOracleSteward
        # will be detected by indexed SvrOracleConfigChanged events and merged in.
        active_svr: dict[str, dict] = {
            addr.lower(): {
                "asset": addr,
                "symbol": sym_name,
                "svr_oracle": _SVR_DUAL_AGGREGATOR,
                "current_oracle": _WETH_PROXY,
                "enabled_block": _SVR_ACTIVATION_BLOCK,
                "source": "DualAggregator (via PriceCapAdapter)",
            }
            for sym_name, addr in _SVR_ASSETS_CORE.items()
        }

        # Merge any SvrOracleSteward config events (future oracle changes)
        _ZERO = "0x0000000000000000000000000000000000000000"
        svr_config_rows = await conn.fetch("""
            SELECT DISTINCT ON (asset)
                asset, current_oracle, svr_oracle, block_number, tx_hash
            FROM svr_oracle_configs
            ORDER BY asset, block_number DESC
        """)
        for r in svr_config_rows:
            if r["svr_oracle"] == _ZERO:
                # Steward disabled SVR for this asset — remove from active set
                active_svr.pop(r["asset"].lower(), None)
            else:
                active_svr[r["asset"].lower()] = {
                    "asset": r["asset"],
                    "symbol": sym_map.get(r["asset"].lower(), r["asset"][:8]),
                    "svr_oracle": r["svr_oracle"],
                    "current_oracle": r["current_oracle"],
                    "enabled_block": int(r["block_number"]),
                    "source": "SvrOracleSteward",
                }

        # Number of SVR config change events ever indexed (steward monitoring)
        total_svr_events = await conn.fetchval("SELECT COUNT(*) FROM svr_oracle_configs") or 0

        # Reserve configs + oracle prices for coverage table
        coverage_rows = await conn.fetch("""
            SELECT
                rc.reserve,
                op.price_usd / 1e8                        AS price_usd,
                rc.ltv / 100.0                            AS ltv,
                rc.liquidation_threshold / 100.0          AS liquidation_threshold,
                rc.liquidation_bonus / 100.0 - 100.0      AS liquidation_bonus
            FROM reserve_configs rc
            JOIN oracle_prices op
                ON op.asset = rc.reserve AND op.snapshot_block = rc.snapshot_block
            WHERE rc.snapshot_block = $1
        """, head)

        # Event volume proxy for supply/debt display in coverage table
        supply_vol = await conn.fetch(
            "SELECT reserve, SUM(amount) AS total FROM aave_supply_events GROUP BY reserve"
        )
        borrow_vol = await conn.fetch(
            "SELECT reserve, SUM(amount) AS total FROM aave_borrow_events GROUP BY reserve"
        )
        sup_map = {r["reserve"].lower(): float(r["total"] or 0) for r in supply_vol}
        bor_map = {r["reserve"].lower(): float(r["total"] or 0) for r in borrow_vol}

        # Liquidation bonus in USD: seized_collateral_usd × liquidation_bonus/(10000+bonus)
        # Uses latest oracle snapshot prices. USDC handled via reserve_configs.decimals.
        liq_rows = await conn.fetch("""
            WITH latest_prices AS (
                SELECT DISTINCT ON (asset)
                    asset, price_usd, snapshot_block
                FROM oracle_prices
                ORDER BY asset, snapshot_block DESC
            ),
            latest_configs AS (
                SELECT DISTINCT ON (reserve)
                    reserve, liquidation_bonus, decimals, snapshot_block
                FROM reserve_configs
                ORDER BY reserve, snapshot_block DESC
            )
            SELECT
                le.collateral_asset,
                COUNT(*)  AS liq_count,
                SUM(
                    (le.liquidated_collateral::numeric / POWER(10, lc.decimals))
                    * (lp.price_usd / 1e8)
                    * lc.liquidation_bonus / (10000.0 + lc.liquidation_bonus)
                ) AS bonus_usd
            FROM aave_liquidation_events le
            JOIN latest_prices lp ON lp.asset = le.collateral_asset
            JOIN latest_configs lc ON lc.reserve = le.collateral_asset
            GROUP BY le.collateral_asset
            ORDER BY bonus_usd DESC NULLS LAST
        """)

        # 30-day liquidation bonus (USD)
        cutoff_30d = max(head_block - 30 * 7200, 0)
        bonus_30d = await conn.fetch("""
            WITH latest_prices AS (
                SELECT DISTINCT ON (asset)
                    asset, price_usd FROM oracle_prices ORDER BY asset, snapshot_block DESC
            ),
            latest_configs AS (
                SELECT DISTINCT ON (reserve)
                    reserve, liquidation_bonus, decimals FROM reserve_configs ORDER BY reserve, snapshot_block DESC
            )
            SELECT
                le.collateral_asset,
                COUNT(*) AS count,
                SUM(
                    (le.liquidated_collateral::numeric / POWER(10, lc.decimals))
                    * (lp.price_usd / 1e8)
                    * lc.liquidation_bonus / (10000.0 + lc.liquidation_bonus)
                ) AS bonus_usd
            FROM aave_liquidation_events le
            JOIN latest_prices lp ON lp.asset = le.collateral_asset
            JOIN latest_configs lc ON lc.reserve = le.collateral_asset
            WHERE le.block_number >= $1
            GROUP BY le.collateral_asset
            ORDER BY bonus_usd DESC NULLS LAST
        """, cutoff_30d)

        # Daily bonus timeline — SVR assets only, from activation block
        svr_active_addrs = list(active_svr.keys())
        if svr_active_addrs:
            daily_rows = await conn.fetch("""
                WITH latest_prices AS (
                    SELECT DISTINCT ON (asset)
                        asset, price_usd FROM oracle_prices ORDER BY asset, snapshot_block DESC
                ),
                latest_configs AS (
                    SELECT DISTINCT ON (reserve)
                        reserve, liquidation_bonus FROM reserve_configs ORDER BY reserve, snapshot_block DESC
                )
                SELECT
                    le.collateral_asset,
                    (25854120 + ((le.block_number - 25854120) / 7200) * 7200) AS day_block,
                    SUM(
                        (le.liquidated_collateral::numeric / 1e18)
                        * (lp.price_usd / 1e8)
                        * lc.liquidation_bonus / (10000.0 + lc.liquidation_bonus)
                    ) AS bonus_usd
                FROM aave_liquidation_events le
                JOIN latest_prices lp ON lp.asset = le.collateral_asset
                JOIN latest_configs lc ON lc.reserve = le.collateral_asset
                WHERE lower(le.collateral_asset) = ANY($1)
                  AND le.block_number >= $2
                GROUP BY le.collateral_asset, day_block
                ORDER BY day_block
            """, svr_active_addrs, _SVR_ACTIVATION_BLOCK)
        else:
            daily_rows = []

    def sym(addr: str) -> str:
        return sym_map.get(addr.lower(), addr[:8])

    # Build coverage table — mark assets with active SVR oracle
    coverage = []
    for r in coverage_rows:
        s = sym(r["reserve"])
        p = float(r["price_usd"])
        decimals = 18 if s != "USDC" else 6
        sup_usd = sup_map.get(r["reserve"].lower(), 0) / (10 ** decimals) * p
        bor_usd = bor_map.get(r["reserve"].lower(), 0) / (10 ** decimals) * p
        svr_info = active_svr.get(r["reserve"].lower())
        coverage.append({
            "asset": s,
            "is_svr": svr_info is not None,
            "svr_oracle": svr_info["svr_oracle"] if svr_info else None,
            "price_usd": p,
            "collateral_usd": sup_usd,
            "debt_usd": bor_usd,
            "ltv": float(r["ltv"]),
            "liquidation_threshold": float(r["liquidation_threshold"]),
            "liquidation_bonus": float(r["liquidation_bonus"]),
        })

    # Aggregate bonus per collateral asset (USD)
    bonus_by_asset: dict[str, dict] = {}
    for r in liq_rows:
        s = sym(r["collateral_asset"])
        is_svr = r["collateral_asset"].lower() in active_svr
        if s not in bonus_by_asset:
            bonus_by_asset[s] = {"count": 0, "bonus_usd": 0.0, "is_svr": is_svr}
        bonus_by_asset[s]["count"] += int(r["liq_count"])
        bonus_by_asset[s]["bonus_usd"] += float(r["bonus_usd"] or 0)

    # 30d bonus (USD)
    bonus_30d_by_asset = {
        sym(r["collateral_asset"]): {
            "count": int(r["count"]),
            "bonus_usd": float(r["bonus_usd"] or 0),
            "is_svr": r["collateral_asset"].lower() in active_svr,
        }
        for r in bonus_30d
    }

    # Daily SVR timeline (USD)
    days_map: dict[int, dict] = {}
    for r in daily_rows:
        db = int(r["day_block"])
        s = sym(r["collateral_asset"])
        if db not in days_map:
            days_map[db] = {}
        days_map[db][s] = float(r["bonus_usd"] or 0)

    head_dt = dt.date.today()
    day_entries = []
    for day_block in sorted(days_map.keys()):
        days_ago = (head_block - day_block) // 7200
        d = head_dt - dt.timedelta(days=int(days_ago))
        day_entries.append({"date": d.isoformat(), **days_map[day_block]})

    assets_seen = list({s for entry in day_entries for s in entry if s != "date"})

    # SVR recapture totals (USD, SVR-active assets only)
    svr_bonus_all_time_usd = sum(
        v["bonus_usd"] for v in bonus_by_asset.values() if v["is_svr"]
    )
    svr_bonus_30d_usd = sum(
        v["bonus_usd"] for v in bonus_30d_by_asset.values() if v["is_svr"]
    )
    svr_liq_count_30d = sum(
        v["count"] for v in bonus_30d_by_asset.values() if v["is_svr"]
    )
    total_bonus_all_time_usd = sum(v["bonus_usd"] for v in bonus_by_asset.values())
    svr_recapture_rate = (
        svr_bonus_all_time_usd / total_bonus_all_time_usd if total_bonus_all_time_usd else 0
    )

    return {
        "svr_active": len(active_svr) > 0,
        "svr_assets": [
            {
                "symbol": info.get("symbol") or sym(info["asset"]),
                "asset": info["asset"],
                "svr_oracle": info["svr_oracle"],
                "current_oracle": info["current_oracle"],
                "enabled_block": info["enabled_block"],
                "source": info.get("source", ""),
            }
            for info in active_svr.values()
        ],
        "steward_address": "0x8b493f416F5F7933cC146b1899c069F2361cad60",
        "dual_aggregator": _SVR_DUAL_AGGREGATOR,
        "svr_activation_block": _SVR_ACTIVATION_BLOCK,
        "total_svr_config_events": int(total_svr_events),
        "svr_bonus_all_time_usd": svr_bonus_all_time_usd,
        "svr_bonus_30d_usd": svr_bonus_30d_usd,
        "svr_liq_count_30d": svr_liq_count_30d,
        "svr_recapture_rate": svr_recapture_rate,
        "coverage": coverage,
        "bonus_all_time": [
            {"asset": k, **v} for k, v in sorted(bonus_by_asset.items(), key=lambda x: -x[1]["bonus_usd"])
        ],
        "bonus_30d": [
            {"asset": k, **v} for k, v in sorted(bonus_30d_by_asset.items(), key=lambda x: -x[1]["bonus_usd"])
        ],
        "daily_bonus": day_entries,
        "daily_assets": assets_seen,
        "snapshot_block": head,
    }

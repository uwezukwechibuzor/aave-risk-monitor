"""FastAPI application entrypoint."""

from __future__ import annotations

import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

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


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    # Startup
    await init_pool(settings.database_url)
    app.state.rpc = EthRPC(
        url=settings.graphops_rpc_url,
        api_key=settings.graphops_api_key,
        max_calls=settings.max_rpc_calls_per_run,
    )
    logger.info("API startup complete.")
    yield
    # Shutdown
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


@app.get("/assets", response_model=AssetsResponse)
async def list_assets() -> AssetsResponse:
    assets_cfg = load_assets_config()
    items = [
        AssetInfo(
            symbol=sym,
            address=cfg["address"],
            decimals=cfg.get("decimals", 18),
        )
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
              AND scenario LIKE $2
              AND block = $3
            ORDER BY scenario
            """,
            asset,
            f"{scenario}:%",
            block,
        )

    shocks: list[ShockResult] = []
    for row in rows:
        sc = str(row["scenario"])
        shock_key = sc.split(":", 1)[1] if ":" in sc else sc
        value = Decimal(str(row["value"]))

        # Look up coverage ratios
        async with pool.acquire() as conn:
            cov_rows = await conn.fetch(
                "SELECT scenario, value FROM signal_values "
                "WHERE signal='coverage_ratio' AND asset=$1 AND block=$2",
                asset,
                block,
            )
        cov_map = {r["scenario"]: Decimal(str(r["value"])) for r in cov_rows}

        shocks.append(
            ShockResult(
                shock=shock_key,
                liquidatable_debt_usd=value,
                coverage_ratio_2pct=cov_map.get("2pct"),
                coverage_ratio_5pct=cov_map.get("5pct"),
            )
        )

    return StressResponse(asset=asset, scenario=scenario, block=block, shocks=shocks)


@app.get("/heatmap/liquidation", response_model=HeatmapResponse)
async def heatmap_liquidation(
    scenario: str = Query("asset_depeg", description="asset_depeg or market_wide"),
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
            SELECT asset, scenario, value
            FROM signal_values
            WHERE signal = 'liquidation_at_risk'
              AND block = $1
              AND scenario LIKE $2
            """,
            block,
            f"{scenario}:%",
        )

    cell_map: dict[tuple[str, str], Decimal] = {}
    for row in rows:
        sc = str(row["scenario"])
        shock = sc.split(":", 1)[1] if ":" in sc else sc
        cell_map[(str(row["asset"]), shock)] = Decimal(str(row["value"]))

    cells = [
        HeatmapCell(row=asset, col=shock, value=cell_map.get((asset, shock), Decimal("0")))
        for asset in asset_symbols
        for shock in shocks
    ]
    return HeatmapResponse(rows=asset_symbols, cols=shocks, cells=cells)


@app.get("/heatmap/health-factor", response_model=HeatmapResponse)
async def heatmap_health_factor(days: int = Query(30, ge=1, le=365)) -> HeatmapResponse:
    from datetime import timedelta

    pool = get_pool()
    cutoff = datetime.now(tz=UTC) - timedelta(days=days)

    bands = ["<1.0", "1.0-1.05", "1.05-1.1", "1.1-1.25", "1.25-1.5", ">1.5"]
    _band_conditions = [
        "health_factor < 1.0",
        "health_factor >= 1.0 AND health_factor < 1.05",
        "health_factor >= 1.05 AND health_factor < 1.1",
        "health_factor >= 1.1 AND health_factor < 1.25",
        "health_factor >= 1.25 AND health_factor < 1.5",
        "health_factor >= 1.5",
    ]

    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT
                date_trunc('day', snapshot_ts) AS day,
                CASE
                    WHEN health_factor < 1.0           THEN '<1.0'
                    WHEN health_factor < 1.05          THEN '1.0-1.05'
                    WHEN health_factor < 1.1           THEN '1.05-1.1'
                    WHEN health_factor < 1.25          THEN '1.1-1.25'
                    WHEN health_factor < 1.5           THEN '1.25-1.5'
                    ELSE '>1.5'
                END AS band,
                SUM(total_debt_base) / 1e8 AS debt_usd
            FROM user_positions
            WHERE snapshot_ts >= $1 AND total_debt_base > 0
            GROUP BY day, band
            ORDER BY day
            """,
            cutoff,
        )

    dates = sorted(set(str(r["day"].date()) for r in rows))
    cells = [
        HeatmapCell(
            row=str(r["band"]),
            col=str(r["day"].date()),
            value=Decimal(str(r["debt_usd"])),
        )
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
            SELECT
                asset,
                EXTRACT(HOUR FROM ts)::int AS hour,
                PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY value) AS median_bps
            FROM signal_values
            WHERE signal = 'oracle_deviation'
              AND scenario LIKE 'spot:%'
              AND ts >= $1
            GROUP BY asset, hour
            ORDER BY asset, hour
            """,
            cutoff,
        )

    cells = [
        HeatmapCell(
            row=str(r["asset"]),
            col=str(r["hour"]),
            value=Decimal(str(r["median_bps"])),
        )
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
            signal=r["signal"],
            asset=r["asset"],
            scenario=r["scenario"],
            block=r["block"],
            value=Decimal(str(r["value"])),
            threshold=Decimal(str(r["threshold"])),
            fired=bool(r["fired"]),
            ts=r["ts"],
            metadata=r["metadata"],
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
                "SELECT * FROM alerts WHERE ts >= $1 ORDER BY ts DESC LIMIT 200",
                since,
            )
        else:
            rows = await conn.fetch("SELECT * FROM alerts ORDER BY ts DESC LIMIT 200")
    items = [
        AlertItem(
            id=r["id"],
            signal=r["signal"],
            asset=r["asset"],
            scenario=r["scenario"],
            severity=r["severity"],
            message=r["message"],
            block=r["block"],
            ts=r["ts"],
            resolved_at=r["resolved_at"],
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
            SELECT
                signal, asset, scenario,
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
        summaries.append(
            BacktestSummary(
                signal=r["signal"],
                asset=r["asset"],
                scenario=r["scenario"],
                fire_count=fires,
                total_count=total,
                fire_rate=fire_rate,
                max_streak=0,  # streaks not recomputed in summary query
                p50=p50,
                p95=p95,
                recommended_threshold=p95,
            )
        )

    return BacktestResponse(summaries=summaries)

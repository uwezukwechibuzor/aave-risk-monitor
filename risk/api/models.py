"""Pydantic response models for API endpoints."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel


class HealthResponse(BaseModel):
    status: str
    database: str
    indexer_lag_blocks: int | None
    rpc_ok: bool
    timestamp: datetime


class AssetInfo(BaseModel):
    symbol: str
    address: str
    decimals: int


class AssetsResponse(BaseModel):
    assets: list[AssetInfo]


class ShockResult(BaseModel):
    shock: str  # e.g. "-0.10"
    liquidatable_debt_usd: Decimal
    coverage_ratio_2pct: Decimal | None
    coverage_ratio_5pct: Decimal | None


class StressResponse(BaseModel):
    asset: str
    scenario: str
    block: int
    shocks: list[ShockResult]


class HeatmapCell(BaseModel):
    row: str
    col: str
    value: Decimal


class HeatmapResponse(BaseModel):
    rows: list[str]
    cols: list[str]
    cells: list[HeatmapCell]


class SignalValue(BaseModel):
    signal: str
    asset: str
    scenario: str
    block: int
    value: Decimal
    threshold: Decimal
    fired: bool
    ts: datetime
    metadata: Any | None = None


class SignalsResponse(BaseModel):
    signals: list[SignalValue]


class AlertItem(BaseModel):
    id: int
    signal: str
    asset: str
    scenario: str
    severity: str
    message: str
    block: int
    ts: datetime
    resolved_at: datetime | None


class AlertsResponse(BaseModel):
    alerts: list[AlertItem]


class BacktestSummary(BaseModel):
    signal: str
    asset: str
    scenario: str
    fire_count: int
    total_count: int
    fire_rate: Decimal
    max_streak: int
    p50: Decimal
    p95: Decimal
    recommended_threshold: Decimal


class BacktestResponse(BaseModel):
    summaries: list[BacktestSummary]

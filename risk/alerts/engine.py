"""Alert engine with deduplication and cooldown.

Flow:
1. After each signal run, query signal_values for fired=true rows since
   the last alert check.
2. For each (signal, asset, scenario), check the alerts table:
   - If the last alert for this key was within cooldown, skip.
   - If the signal has recovered (fired=false now), send a "resolved" alert.
3. Classify severity: warning or critical per thresholds.
4. Send via Telegram if configured; always write to alerts table.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import asyncpg

from risk.config import Settings

logger = logging.getLogger(__name__)

DEFAULT_COOLDOWN_SECONDS = 21600  # 6 hours


async def evaluate_and_alert(
    db_pool: asyncpg.Pool,
    settings: Settings,
    block: int,
    signals_config: dict[str, Any],
) -> int:
    """Check fired signals and send new alerts. Returns count of alerts sent."""
    cooldown = timedelta(
        seconds=signals_config.get("alert_cooldown_seconds", DEFAULT_COOLDOWN_SECONDS)
    )

    async with db_pool.acquire() as conn:
        # Get latest fired signals at this block
        rows = await conn.fetch(
            """
            SELECT signal, asset, scenario, value, threshold, fired, metadata
            FROM signal_values
            WHERE block = $1
            ORDER BY signal, asset, scenario
            """,
            block,
        )

    sent = 0
    for row in rows:
        signal = row["signal"]
        asset = row["asset"]
        scenario = row["scenario"]
        value = Decimal(str(row["value"]))
        threshold = Decimal(str(row["threshold"]))
        fired = bool(row["fired"])

        severity = _classify_severity(signal, value, signals_config)

        if fired:
            # Check cooldown
            if await _is_in_cooldown(db_pool, signal, asset, scenario, cooldown):
                continue
            message = _format_alert_message(
                signal, asset, scenario, value, threshold, block, severity
            )
            await _save_alert(db_pool, signal, asset, scenario, severity, message, block)
            await _send_alert(message, settings)
            sent += 1
        else:
            # Check if we need to send a "resolved" alert
            async with db_pool.acquire() as conn:
                last = await conn.fetchrow(
                    """
                    SELECT id, resolved_at FROM alerts
                    WHERE signal = $1 AND asset = $2 AND scenario = $3
                      AND resolved_at IS NULL
                    ORDER BY ts DESC LIMIT 1
                    """,
                    signal,
                    asset,
                    scenario,
                )
            if last:
                async with db_pool.acquire() as conn:
                    await conn.execute(
                        "UPDATE alerts SET resolved_at = now() WHERE id = $1",
                        last["id"],
                    )
                resolved_msg = (
                    f"[RESOLVED] {signal} / {asset} / {scenario}: "
                    f"signal no longer firing (value={value:.4f}, threshold={threshold:.4f})."
                )
                await _send_alert(resolved_msg, settings)
                sent += 1

    return sent


def _classify_severity(
    signal: str,
    value: Decimal,
    config: dict[str, Any],
) -> str:
    signal_cfg = config.get(signal, {})
    # Prefer the _usd variant if present, then the plain key
    crit = signal_cfg.get("critical_threshold_usd") or signal_cfg.get("critical_threshold")
    if crit is not None and value >= Decimal(str(crit)):
        return "critical"
    return "warning"


def _format_alert_message(
    signal: str,
    asset: str,
    scenario: str,
    value: Decimal,
    threshold: Decimal,
    block: int,
    severity: str,
) -> str:
    sev_emoji = "[CRITICAL]" if severity == "critical" else "[WARNING]"
    return (
        f"{sev_emoji} {asset} | {signal} | {scenario}\n"
        f"Value: {value:.4f} | Threshold: {threshold:.4f}\n"
        f"Block: {block:,}"
    )


async def _is_in_cooldown(
    db_pool: asyncpg.Pool,
    signal: str,
    asset: str,
    scenario: str,
    cooldown: timedelta,
) -> bool:
    cutoff = datetime.now(tz=UTC) - cooldown
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT ts FROM alerts
            WHERE signal = $1 AND asset = $2 AND scenario = $3 AND ts > $4
            ORDER BY ts DESC LIMIT 1
            """,
            signal,
            asset,
            scenario,
            cutoff,
        )
    return row is not None


async def _save_alert(
    db_pool: asyncpg.Pool,
    signal: str,
    asset: str,
    scenario: str,
    severity: str,
    message: str,
    block: int,
) -> None:
    async with db_pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO alerts (signal, asset, scenario, severity, message, block)
            VALUES ($1,$2,$3,$4,$5,$6)
            """,
            signal,
            asset,
            scenario,
            severity,
            message,
            block,
        )


async def _send_alert(message: str, settings: Settings) -> None:
    if settings.telegram_bot_token and settings.telegram_chat_id:
        await _send_telegram(message, settings.telegram_bot_token, settings.telegram_chat_id)
    else:
        logger.warning("ALERT: %s", message)
        print(f"[ALERT] {message}")


async def _send_telegram(message: str, token: str, chat_id: str) -> None:
    import httpx

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            await client.post(url, json={"chat_id": chat_id, "text": message})
    except Exception as e:
        logger.error("Telegram send failed: %s", e)

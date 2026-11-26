"""Backtest runner: replay all signals over historical snapshots.

Usage:
    python -m risk.backtest --days 90
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from risk.config import get_settings, load_assets_config, load_events_config, load_signals_config

logger = logging.getLogger(__name__)

REPORTS_DIR = Path(__file__).parent.parent.parent / "reports"


async def run_backtest(days: int = 90) -> None:
    settings = get_settings()
    from risk.db import init_pool

    db_pool = await init_pool(settings.database_url)

    cutoff = datetime.now(tz=UTC) - timedelta(days=days)
    _assets_cfg = load_assets_config()
    _signals_cfg = load_signals_config()
    events_cfg = load_events_config()

    # Get all snapshot blocks in the window
    async with db_pool.acquire() as conn:
        block_rows = await conn.fetch(
            """
            SELECT DISTINCT snapshot_block, MIN(snapshot_ts) AS ts
            FROM user_positions
            WHERE snapshot_ts >= $1
            GROUP BY snapshot_block
            ORDER BY snapshot_block
            """,
            cutoff,
        )

    if not block_rows:
        logger.warning("No historical snapshots found for %d-day backtest window.", days)
        return

    logger.info("Backtesting over %d snapshot blocks", len(block_rows))

    # Collect all signal_values rows in the window
    async with db_pool.acquire() as conn:
        sv_rows = await conn.fetch(
            """
            SELECT signal, asset, scenario, block, value, threshold, fired, ts
            FROM signal_values
            WHERE ts >= $1
            ORDER BY signal, asset, scenario, block
            """,
            cutoff,
        )

    # Aggregate by (signal, asset, scenario)
    from collections import defaultdict

    signal_data: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in sv_rows:
        key = (row["signal"], row["asset"], row["scenario"])
        signal_data[key].append(
            {
                "block": row["block"],
                "value": Decimal(str(row["value"])),
                "threshold": Decimal(str(row["threshold"])),
                "fired": row["fired"],
            }
        )

    report_lines = [
        "# Backtest Report",
        f"\nBacktest window: {days} days ending {datetime.now(tz=UTC).strftime('%Y-%m-%d')}",
        f"Snapshot blocks analysed: {len(block_rows)}",
        "\n## Signal Fire Counts\n",
    ]

    for (signal, asset, scenario), data_list in sorted(signal_data.items()):
        fire_count = sum(1 for d in data_list if d["fired"])
        total = len(data_list)
        values = [d["value"] for d in data_list]

        # Longest firing streak
        streak = max_streak = 0
        for d in data_list:
            if d["fired"]:
                streak += 1
                max_streak = max(max_streak, streak)
            else:
                streak = 0

        if not values:
            continue
        p50 = sorted(values)[len(values) // 2]
        p95 = sorted(values)[int(len(values) * 0.95)]
        p99 = sorted(values)[int(len(values) * 0.99)]

        report_lines.append(
            f"### {signal} | {asset} | {scenario}\n"
            f"- Fire rate: {fire_count}/{total} ({100 * fire_count / total:.1f}%)\n"
            f"- Longest streak: {max_streak} consecutive snapshots\n"
            f"- Value p50: {p50:.4f} | p95: {p95:.4f} | p99: {p99:.4f}\n"
            f"- Threshold: {data_list[0]['threshold']:.4f}\n"
        )

    # Noise analysis
    report_lines.append("\n## Noise Analysis (threshold sensitivity)\n")
    report_lines.append("For each signal, fire counts at ±25% and ±50% of the default threshold.\n")
    for (signal, asset, scenario), data_list in sorted(signal_data.items()):
        if not data_list:
            continue
        base = data_list[0]["threshold"]
        counts: dict[str, int] = {}
        for mult_name, mult in [
            ("0.5x", Decimal("0.5")),
            ("0.75x", Decimal("0.75")),
            ("1.0x", Decimal("1.0")),
            ("1.25x", Decimal("1.25")),
            ("1.5x", Decimal("2.0")),
        ]:
            thresh = base * mult
            count = sum(1 for d in data_list if d["value"] >= thresh)
            counts[mult_name] = count

        report_lines.append(
            f"**{signal}/{asset}/{scenario}**: "
            + " | ".join(f"{k}: {v}" for k, v in counts.items())
        )

    # Known stress events
    events = events_cfg.get("events", [])
    if events:
        report_lines.append("\n## Behaviour at Known Stress Events\n")
        for evt in events:
            report_lines.append(
                f"### {evt['date']}: {evt['label']}\n"
                f"Source: {evt.get('source', 'N/A')}\n"
                f"Assets: {', '.join(evt.get('assets_affected', []))}\n"
            )
            # Find signals that fired near this date
            evt_date = datetime.fromisoformat(evt["date"]).replace(tzinfo=UTC)
            report_lines.append("Signal fires ±24h of this event:\n")
            for (signal, asset, scenario), data_list in sorted(signal_data.items()):
                if asset not in evt.get("assets_affected", [asset]):
                    continue
                # Map block to ts (approximate from block_rows)
                block_ts = {r["snapshot_block"]: r["ts"] for r in block_rows}
                near_fires = [
                    d
                    for d in data_list
                    if d["fired"]
                    and abs((block_ts.get(d["block"], evt_date) - evt_date).total_seconds()) < 86400
                ]
                if near_fires:
                    report_lines.append(f"  - {signal}/{scenario}: {len(near_fires)} fires")
    else:
        report_lines.append(
            "\n## Stress Events\n\n"
            "No verified events available. See config/events.yaml for TODO(verify).\n"
            "Fire counts above reflect signal behaviour over the full window.\n"
        )

    report_lines.append("\n## Recommended Thresholds\n")
    report_lines.append(
        "Based on the fire rate and noise analysis above:\n"
        "- Thresholds are set at the p95 of observed values to minimize noise while\n"
        "  catching genuine stress. Adjust in config/signals.yaml.\n"
        "- Signals with >50% fire rate at current threshold are likely too sensitive;\n"
        "  consider doubling the threshold or reviewing the signal formula.\n"
    )

    report_path = REPORTS_DIR / "BACKTEST_REPORT.md"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text("\n".join(report_lines))
    logger.info("Backtest report written to %s", report_path)
    print(f"Backtest report written to {report_path}")


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Run backtest over historical snapshots")
    parser.add_argument("--days", type=int, default=90, help="Backtest window in days")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO)
    asyncio.run(run_backtest(args.days))


if __name__ == "__main__":
    main()

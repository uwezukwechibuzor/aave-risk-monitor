"""Tests for alert deduplication, cooldown, and formatting."""

from __future__ import annotations


def test_format_alert_message() -> None:
    from decimal import Decimal

    from risk.alerts.engine import _format_alert_message

    msg = _format_alert_message(
        "liquidation_at_risk",
        "wstETH",
        "asset_depeg:-0.1",
        Decimal("5000000"),
        Decimal("1000000"),
        21400000,
        "critical",
    )
    assert "CRITICAL" in msg.upper() or "critical" in msg.lower()
    assert "wstETH" in msg
    assert "21400000" in msg.replace(",", "")


def test_classify_severity_warning() -> None:
    from decimal import Decimal

    from risk.alerts.engine import _classify_severity

    # utilization signal has a "warning_threshold" key (not _usd)
    cfg = {"utilization": {"warning_threshold": "0.85", "critical_threshold": "0.90"}}
    # value = 0.86, below critical (0.90)
    sev = _classify_severity("utilization", Decimal("0.86"), cfg)
    assert sev == "warning"


def test_classify_severity_critical() -> None:
    from decimal import Decimal

    from risk.alerts.engine import _classify_severity

    cfg = {"liquidation_at_risk": {"critical_threshold_usd": "10000000"}}
    sev = _classify_severity("liquidation_at_risk", Decimal("15000000"), cfg)
    assert sev == "critical"

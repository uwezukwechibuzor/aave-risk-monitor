"""Unit tests for risk/signals/math.py.

All fixtures are hand-checked against Aave v3 documentation and the
close-factor rules in LiquidationLogic.sol.
"""

from __future__ import annotations

from decimal import Decimal

from risk.signals.math import (
    CLOSE_FACTOR_HF_THRESHOLD,
    DEFAULT_CLOSE_FACTOR,
    MAX_CLOSE_FACTOR,
    UserPosition,
    close_factor,
    coverage_ratio,
    hhi,
    liquidatable_debt,
    oracle_deviation_bps,
    shocked_health_factor,
)

# ── Helpers ────────────────────────────────────────────────────────────────────


def _pos(
    total_coll: float,
    total_debt: float,
    liq_thresh: float = 8250,  # 82.50%
    hf: float = 1.5,
    emode: int = 0,
) -> UserPosition:
    return UserPosition(
        total_collateral_base=Decimal(str(total_coll)),
        total_debt_base=Decimal(str(total_debt)),
        current_liquidation_threshold=Decimal(str(liq_thresh)),
        health_factor=Decimal(str(hf)),
        emode_category=emode,
    )


# ── close_factor ──────────────────────────────────────────────────────────────


class TestCloseFactor:
    def test_above_threshold(self) -> None:
        assert close_factor(Decimal("1.0")) == DEFAULT_CLOSE_FACTOR
        assert close_factor(Decimal("1.5")) == DEFAULT_CLOSE_FACTOR
        assert close_factor(Decimal("0.95")) == DEFAULT_CLOSE_FACTOR

    def test_below_threshold(self) -> None:
        assert close_factor(Decimal("0.94")) == MAX_CLOSE_FACTOR
        assert close_factor(Decimal("0.0")) == MAX_CLOSE_FACTOR
        assert close_factor(Decimal("0.5")) == MAX_CLOSE_FACTOR

    def test_exactly_at_threshold(self) -> None:
        """Aave uses >= threshold → DEFAULT_CLOSE_FACTOR (50%)."""
        assert close_factor(CLOSE_FACTOR_HF_THRESHOLD) == DEFAULT_CLOSE_FACTOR

    def test_just_below_threshold(self) -> None:
        below = CLOSE_FACTOR_HF_THRESHOLD - Decimal("0.001")
        assert close_factor(below) == MAX_CLOSE_FACTOR


# ── shocked_health_factor ──────────────────────────────────────────────────────


class TestShockedHealthFactor:
    def test_no_shock_unchanged(self) -> None:
        """With a 0% shock, HF should stay the same (within floating precision)."""
        pos = _pos(total_coll=10000, total_debt=5000, liq_thresh=8250, hf=1.65)
        price = Decimal("2000")
        # collateral in this asset = all collateral for simplicity
        asset_coll = Decimal("10000")
        result = shocked_health_factor(pos, price, price, asset_coll)
        # HF = (10000 * 0.825) / 5000 = 8250/5000 = 1.65
        assert abs(result - Decimal("1.65")) < Decimal("0.001")

    def test_10pct_shock_reduces_hf(self) -> None:
        pos = _pos(10000, 5000, 8250, 1.65)
        price = Decimal("2000")
        shocked = price * Decimal("0.9")
        result = shocked_health_factor(pos, price, shocked, Decimal("10000"))
        # new liq coll = 10000 * 0.9 * 0.825 = 7425; HF = 7425/5000 = 1.485
        assert abs(result - Decimal("1.485")) < Decimal("0.001")

    def test_total_depeg_hf_zero(self) -> None:
        pos = _pos(10000, 5000, 8250, 1.65)
        price = Decimal("2000")
        result = shocked_health_factor(pos, price, Decimal("0"), Decimal("10000"))
        assert result == Decimal("0")

    def test_no_debt_returns_max(self) -> None:
        pos = _pos(10000, 0, 8250, 999)
        result = shocked_health_factor(pos, Decimal("2000"), Decimal("1800"), Decimal("10000"))
        assert result == Decimal("999")

    def test_emode_threshold_applied(self) -> None:
        """In e-mode, the e-mode threshold (93%) is used instead of reserve's 82.5%."""
        pos = _pos(10000, 5000, 8250, 1.65, emode=1)
        price = Decimal("2000")
        # With emode LT 9300:
        # liq coll = 10000 * 0.9300 = 9300; HF = 9300/5000 = 1.86
        result = shocked_health_factor(
            pos, price, price, Decimal("10000"), emode_liq_threshold=Decimal("9300")
        )
        assert abs(result - Decimal("1.86")) < Decimal("0.001")

    def test_partial_collateral_shock(self) -> None:
        """Only half the collateral is in the shocked asset."""
        pos = _pos(10000, 5000, 8250, 1.65)
        price = Decimal("2000")
        shocked = Decimal("1600")  # -20%
        # original contribution = 5000 * 0.825 = 4125
        # shocked contribution = 5000 * 0.8 * 0.825 = 3300
        # original total liq coll = 10000 * 0.825 = 8250
        # new = 8250 - 4125 + 3300 = 7425; HF = 7425/5000 = 1.485
        result = shocked_health_factor(pos, price, shocked, Decimal("5000"))
        assert abs(result - Decimal("1.485")) < Decimal("0.001")


# ── liquidatable_debt ─────────────────────────────────────────────────────────


class TestLiquidatableDebt:
    def test_hf_above_1_is_zero(self) -> None:
        pos = _pos(10000, 5000, 8250, 1.5)
        result = liquidatable_debt(pos, Decimal("1.0"))
        assert result == Decimal("0")

    def test_hf_above_threshold_is_50pct(self) -> None:
        """HF between 0.95 and 1: close factor = 50%."""
        pos = _pos(10000, 5000, 8250, 1.5)
        result = liquidatable_debt(pos, Decimal("0.98"))
        assert result == Decimal("2500")  # 50% of 5000

    def test_hf_below_threshold_is_100pct(self) -> None:
        """HF below 0.95: close factor = 100%."""
        pos = _pos(10000, 5000, 8250, 1.5)
        result = liquidatable_debt(pos, Decimal("0.80"))
        assert result == Decimal("5000")  # 100% of 5000

    def test_zero_debt(self) -> None:
        pos = _pos(10000, 0, 8250, 999)
        result = liquidatable_debt(pos, Decimal("0.5"))
        assert result == Decimal("0")


# ── coverage_ratio ────────────────────────────────────────────────────────────


class TestCoverageRatio:
    def test_sufficient_coverage(self) -> None:
        ratio = coverage_ratio(Decimal("5000000"), Decimal("1000000"))
        assert ratio == Decimal("5")

    def test_insufficient_coverage(self) -> None:
        ratio = coverage_ratio(Decimal("500000"), Decimal("1000000"))
        assert ratio == Decimal("0.5")

    def test_zero_liquidatable(self) -> None:
        ratio = coverage_ratio(Decimal("1000000"), Decimal("0"))
        assert ratio == Decimal("999")  # no debt = no risk

    def test_zero_capacity(self) -> None:
        ratio = coverage_ratio(Decimal("0"), Decimal("1000000"))
        assert ratio == Decimal("0")

    def test_equal(self) -> None:
        ratio = coverage_ratio(Decimal("1000000"), Decimal("1000000"))
        assert ratio == Decimal("1")


# ── oracle_deviation_bps ──────────────────────────────────────────────────────


class TestOracleDeviationBps:
    def test_no_deviation(self) -> None:
        assert oracle_deviation_bps(Decimal("2000"), Decimal("2000")) == Decimal("0")

    def test_1pct_above(self) -> None:
        """Oracle 1% above market = +100 bps."""
        result = oracle_deviation_bps(Decimal("2020"), Decimal("2000"))
        assert result == Decimal("100")

    def test_1pct_below(self) -> None:
        """Oracle 1% below market = -100 bps."""
        result = oracle_deviation_bps(Decimal("1980"), Decimal("2000"))
        assert result == Decimal("-100")

    def test_zero_market_price(self) -> None:
        assert oracle_deviation_bps(Decimal("100"), Decimal("0")) == Decimal("0")


# ── hhi ───────────────────────────────────────────────────────────────────────


class TestHHI:
    def test_monopoly(self) -> None:
        """One wallet holds everything → HHI = 1."""
        assert hhi([Decimal("1")]) == Decimal("1")

    def test_equal_duopoly(self) -> None:
        """Two equal wallets → HHI = 0.5."""
        result = hhi([Decimal("0.5"), Decimal("0.5")])
        assert result == Decimal("0.5")

    def test_equal_10(self) -> None:
        """Ten equal wallets → HHI = 0.1."""
        shares = [Decimal("0.1")] * 10
        result = hhi(shares)
        assert abs(result - Decimal("0.1")) < Decimal("0.0001")

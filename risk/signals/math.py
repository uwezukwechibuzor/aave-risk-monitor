"""Core risk maths: health factor shock, close factor, coverage ratio.

All functions are pure (no I/O) so they can be unit-tested cheaply.

Aave v3 close-factor rules (verified against Aave v3 protocol source and docs):
- If health factor >= CLOSE_FACTOR_HF_THRESHOLD: close factor = 50% (MAX_LIQUIDATION_CLOSE_FACTOR / 2)
- If health factor <  CLOSE_FACTOR_HF_THRESHOLD: close factor = 100%

Source: https://github.com/bgd-labs/aave-v3-origin/blob/main/src/core/contracts/protocol/libraries/logic/LiquidationLogic.sol
See: DEFAULT_LIQUIDATION_CLOSE_FACTOR = 5000 (50% in bps)
     MAX_LIQUIDATION_CLOSE_FACTOR = 10000 (100% in bps)
     CLOSE_FACTOR_HF_THRESHOLD = 0.95e18

RAY = 1e27 (Aave internal representation for health factor)
"""

from __future__ import annotations

from decimal import Decimal
from typing import NamedTuple

RAY = Decimal("1e27")
WAD = Decimal("1e18")

# Aave v3 close-factor threshold.
# Below this HF, the entire position can be liquidated (100% close factor).
# Source: LiquidationLogic.sol CLOSE_FACTOR_HF_THRESHOLD = 0.95e18
CLOSE_FACTOR_HF_THRESHOLD = Decimal("0.95")

DEFAULT_CLOSE_FACTOR = Decimal("0.5")  # 50%
MAX_CLOSE_FACTOR = Decimal("1.0")  # 100%


class UserPosition(NamedTuple):
    total_collateral_base: Decimal  # USD, 8-decimal base currency
    total_debt_base: Decimal
    current_liquidation_threshold: Decimal  # e.g. 8250 (= 82.50%)
    health_factor: Decimal  # already in decimal (e.g. 1.23)
    emode_category: int


def close_factor(health_factor: Decimal) -> Decimal:
    """Return the liquidatable fraction (0.5 or 1.0) for a given health factor.

    Per Aave v3 LiquidationLogic: 50% if HF >= 0.95, else 100%.
    """
    if health_factor < CLOSE_FACTOR_HF_THRESHOLD:
        return MAX_CLOSE_FACTOR
    return DEFAULT_CLOSE_FACTOR


def shocked_health_factor(
    pos: UserPosition,
    asset_price_usd: Decimal,
    shocked_price_usd: Decimal,
    asset_collateral_base: Decimal,
    emode_liq_threshold: Decimal | None = None,
) -> Decimal:
    """Recompute health factor with a shocked asset price.

    Formula:
        HF = sum(collateral_i * liq_threshold_i) / total_debt_base

    We only shock the target asset.  For the shocked asset:
        shocked_collateral_value = original_value * (shocked_price / original_price)

    Then:
        new_liq_collateral = total_liq_collateral
                           - (asset_collateral_base * pos.current_liquidation_threshold / 10000)
                           + (asset_collateral_base * shocked_price / original_price
                              * effective_liq_threshold / 10000)

    Parameters:
        pos: current position snapshot
        asset_price_usd: current oracle price for the asset (8 decimals, same unit as base)
        shocked_price_usd: hypothetical shocked price
        asset_collateral_base: collateral value of this asset in base currency units
        emode_liq_threshold: if user is in e-mode and this asset participates,
                             use this threshold instead of the reserve's threshold
    """
    if pos.total_debt_base == 0:
        return Decimal("999")

    # Effective threshold for this asset (e-mode overrides reserve default for
    # the target asset only; other assets in the position still use the position's
    # aggregate threshold).
    liq_threshold = (
        emode_liq_threshold
        if emode_liq_threshold is not None
        else pos.current_liquidation_threshold
    )

    # When e-mode is active, the position's stored current_liquidation_threshold
    # already reflects the e-mode threshold.  When we override it here (for
    # out-of-emode callers passing the emode threshold explicitly), we also
    # need to recompute the total using the new threshold to stay consistent.
    effective_position_liq_threshold = (
        emode_liq_threshold
        if emode_liq_threshold is not None
        else pos.current_liquidation_threshold
    )
    total_liq_collateral = (
        pos.total_collateral_base * effective_position_liq_threshold / Decimal("10000")
    )

    if asset_price_usd <= 0:
        return Decimal("0")

    price_ratio = shocked_price_usd / asset_price_usd

    # Remove original contribution, add shocked contribution
    original_contribution = asset_collateral_base * liq_threshold / Decimal("10000")
    shocked_contribution = asset_collateral_base * price_ratio * liq_threshold / Decimal("10000")

    new_liq_collateral = total_liq_collateral - original_contribution + shocked_contribution

    if new_liq_collateral <= 0:
        return Decimal("0")

    return new_liq_collateral / pos.total_debt_base


def liquidatable_debt(
    pos: UserPosition,
    shocked_hf: Decimal,
) -> Decimal:
    """Estimate the USD amount of debt liquidatable given a shocked health factor.

    Uses Aave v3 close-factor rules.
    Returns 0 if the shocked HF is >= 1 (not liquidatable).
    """
    if shocked_hf >= Decimal("1"):
        return Decimal("0")
    cf = close_factor(shocked_hf)
    return pos.total_debt_base * cf


def coverage_ratio(dex_capacity_usd: Decimal, liquidatable_usd: Decimal) -> Decimal:
    """Ratio of DEX liquidation capacity to liquidatable debt.

    > 1 means DEX can absorb all liquidations.
    < 1 means there is a shortfall.
    """
    if liquidatable_usd <= 0:
        return Decimal("999")
    if dex_capacity_usd <= 0:
        return Decimal("0")
    return dex_capacity_usd / liquidatable_usd


def oracle_deviation_bps(oracle_price: Decimal, market_price: Decimal) -> Decimal:
    """Signed deviation of oracle from market in basis points.

    Positive = oracle above market (overpriced), negative = below.
    """
    if market_price <= 0:
        return Decimal("0")
    return (oracle_price - market_price) / market_price * Decimal("10000")


def hhi(shares: list[Decimal]) -> Decimal:
    """Herfindahl-Hirschman Index from a list of market shares (0-1 each)."""
    return sum(s**2 for s in shares)

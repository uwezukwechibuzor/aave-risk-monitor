// SPDX-License-Identifier: MIT
pragma solidity ^0.8.26;

import {AccessControl} from "@openzeppelin/contracts/access/AccessControl.sol";

/**
 * @title DeviationGuard
 * @notice Demo circuit-breaker for oracle price deviation monitoring.
 *
 * A keeper submits (asset, oraclePrice, marketPrice) reports.  The contract
 * computes the deviation in basis points with explicit, documented rounding,
 * and flags the asset when the deviation exceeds the asset's threshold for
 * N consecutive reports.
 *
 * @dev NOT deployed on-chain.  This is a demonstration contract for the
 *      LlamaRisk risk-engineering portfolio project.
 *
 * Access control:
 *   GUARDIAN_ROLE - can set thresholds and consecutiveLimit, and unflag assets
 *   KEEPER_ROLE   - can submit price reports
 *
 * Design decisions:
 *   - Thresholds are in basis points (1 bps = 0.01%).
 *   - Deviation is |oracle - market| / market * 10_000, rounded DOWN (floor).
 *     Rounding down is conservative: it slightly underestimates deviation,
 *     requiring the signal to be unambiguously above threshold before firing.
 *   - Unflagging requires a guardian action to prevent reflexive recovery
 *     during stress (a temporary price bounce should not auto-clear an alert).
 *   - Thresholds are bounded: [MIN_THRESHOLD_BPS, MAX_THRESHOLD_BPS] to
 *     prevent misconfiguration.
 */
contract DeviationGuard is AccessControl {
    // ── Roles ────────────────────────────────────────────────────────────────
    bytes32 public constant GUARDIAN_ROLE = keccak256("GUARDIAN_ROLE");
    bytes32 public constant KEEPER_ROLE = keccak256("KEEPER_ROLE");

    // ── Constants ─────────────────────────────────────────────────────────────
    uint256 public constant MIN_THRESHOLD_BPS = 10;    // 0.10%
    uint256 public constant MAX_THRESHOLD_BPS = 10_000; // 100% (full depeg)
    uint256 public constant BPS_DENOMINATOR = 10_000;

    // ── State ─────────────────────────────────────────────────────────────────
    struct AssetConfig {
        uint256 thresholdBps;        // deviation threshold in bps
        uint256 consecutiveRequired; // consecutive reports above threshold to flag
    }

    struct AssetState {
        uint256 consecutiveCount; // consecutive reports above threshold
        bool isFlagged;
    }

    uint256 public defaultThresholdBps;
    uint256 public defaultConsecutiveRequired;

    mapping(address => AssetConfig) private _configs;
    mapping(address => AssetState) private _states;

    // ── Events ────────────────────────────────────────────────────────────────
    event ReportSubmitted(
        address indexed asset,
        uint256 oraclePrice,
        uint256 marketPrice,
        uint256 deviationBps,
        bool thresholdExceeded
    );
    event AssetFlagged(address indexed asset, uint256 deviationBps, uint256 consecutiveCount);
    event AssetUnflagged(address indexed asset);
    event ThresholdUpdated(address indexed asset, uint256 thresholdBps);
    event ConsecutiveLimitUpdated(address indexed asset, uint256 consecutiveRequired);
    event DefaultThresholdUpdated(uint256 thresholdBps);
    event DefaultConsecutiveUpdated(uint256 consecutiveRequired);

    // ── Errors ────────────────────────────────────────────────────────────────
    error ThresholdOutOfBounds(uint256 provided, uint256 min, uint256 max);
    error ConsecutiveMustBePositive();
    error ZeroPrice();

    // ── Constructor ───────────────────────────────────────────────────────────
    constructor(
        address guardian,
        uint256 _defaultThresholdBps,
        uint256 _defaultConsecutiveRequired
    ) {
        _validateThreshold(_defaultThresholdBps);
        if (_defaultConsecutiveRequired == 0) revert ConsecutiveMustBePositive();

        _grantRole(DEFAULT_ADMIN_ROLE, guardian);
        _grantRole(GUARDIAN_ROLE, guardian);

        defaultThresholdBps = _defaultThresholdBps;
        defaultConsecutiveRequired = _defaultConsecutiveRequired;
    }

    // ── External: keeper ──────────────────────────────────────────────────────

    /**
     * @notice Submit a price observation for an asset.
     * @param asset       The asset address being monitored.
     * @param oraclePrice The oracle's current price (any consistent unit).
     * @param marketPrice The DEX or market's current price (same unit as oraclePrice).
     *
     * Rounding: deviation = floor(|oracle - market| * BPS_DENOMINATOR / market).
     * We use integer floor division.  This slightly underestimates deviation,
     * so the threshold is never triggered spuriously by rounding artefacts.
     */
    function submit(
        address asset,
        uint256 oraclePrice,
        uint256 marketPrice
    ) external onlyRole(KEEPER_ROLE) {
        if (marketPrice == 0) revert ZeroPrice();
        if (oraclePrice == 0) revert ZeroPrice();

        uint256 deviationBps = _computeDeviationBps(oraclePrice, marketPrice);

        AssetConfig memory cfg = _effectiveConfig(asset);
        AssetState storage state = _states[asset];

        bool exceeded = deviationBps > cfg.thresholdBps;
        if (exceeded) {
            state.consecutiveCount += 1;
        } else {
            // Reset streak on any non-exceeding report
            state.consecutiveCount = 0;
        }

        if (!state.isFlagged && state.consecutiveCount >= cfg.consecutiveRequired) {
            state.isFlagged = true;
            emit AssetFlagged(asset, deviationBps, state.consecutiveCount);
        }

        emit ReportSubmitted(asset, oraclePrice, marketPrice, deviationBps, exceeded);
    }

    // ── External: guardian ────────────────────────────────────────────────────

    /**
     * @notice Manually unflag an asset.  Only a guardian can clear a flag.
     * @dev Intentionally NOT automatic: a reflexive price bounce during stress
     *      should not auto-clear an alert.  The guardian must review context.
     */
    function unflag(address asset) external onlyRole(GUARDIAN_ROLE) {
        AssetState storage state = _states[asset];
        state.isFlagged = false;
        state.consecutiveCount = 0;
        emit AssetUnflagged(asset);
    }

    /**
     * @notice Set a per-asset threshold.  Reverts if out of bounds.
     */
    function setThreshold(address asset, uint256 thresholdBps) external onlyRole(GUARDIAN_ROLE) {
        _validateThreshold(thresholdBps);
        _configs[asset].thresholdBps = thresholdBps;
        emit ThresholdUpdated(asset, thresholdBps);
    }

    /**
     * @notice Set a per-asset consecutive-report requirement.
     */
    function setConsecutiveRequired(address asset, uint256 n) external onlyRole(GUARDIAN_ROLE) {
        if (n == 0) revert ConsecutiveMustBePositive();
        _configs[asset].consecutiveRequired = n;
        emit ConsecutiveLimitUpdated(asset, n);
    }

    function setDefaultThreshold(uint256 thresholdBps) external onlyRole(GUARDIAN_ROLE) {
        _validateThreshold(thresholdBps);
        defaultThresholdBps = thresholdBps;
        emit DefaultThresholdUpdated(thresholdBps);
    }

    function setDefaultConsecutive(uint256 n) external onlyRole(GUARDIAN_ROLE) {
        if (n == 0) revert ConsecutiveMustBePositive();
        defaultConsecutiveRequired = n;
        emit DefaultConsecutiveUpdated(n);
    }

    // ── External: views ───────────────────────────────────────────────────────

    function isFlagged(address asset) external view returns (bool) {
        return _states[asset].isFlagged;
    }

    function consecutiveCount(address asset) external view returns (uint256) {
        return _states[asset].consecutiveCount;
    }

    function effectiveThresholdBps(address asset) external view returns (uint256) {
        return _effectiveConfig(asset).thresholdBps;
    }

    function effectiveConsecutiveRequired(address asset) external view returns (uint256) {
        return _effectiveConfig(asset).consecutiveRequired;
    }

    // ── Internal ──────────────────────────────────────────────────────────────

    /**
     * @dev Compute |oracle - market| / market in basis points, floor division.
     *
     *   deviation_bps = floor(|oracle - market| * 10_000 / market)
     *
     * Overflow safety: oraclePrice and marketPrice are prices (< 2^128 in practice),
     * so the product fits in uint256.
     */
    function _computeDeviationBps(
        uint256 oraclePrice,
        uint256 marketPrice
    ) internal pure returns (uint256) {
        uint256 diff = oraclePrice > marketPrice
            ? oraclePrice - marketPrice
            : marketPrice - oraclePrice;
        return (diff * BPS_DENOMINATOR) / marketPrice;
    }

    function _effectiveConfig(address asset) internal view returns (AssetConfig memory) {
        AssetConfig memory cfg = _configs[asset];
        if (cfg.thresholdBps == 0) cfg.thresholdBps = defaultThresholdBps;
        if (cfg.consecutiveRequired == 0) cfg.consecutiveRequired = defaultConsecutiveRequired;
        return cfg;
    }

    function _validateThreshold(uint256 bps) internal pure {
        if (bps < MIN_THRESHOLD_BPS || bps > MAX_THRESHOLD_BPS) {
            revert ThresholdOutOfBounds(bps, MIN_THRESHOLD_BPS, MAX_THRESHOLD_BPS);
        }
    }
}

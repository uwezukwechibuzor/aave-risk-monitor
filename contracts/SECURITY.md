# DeviationGuard Security Model

## Threat Model

### What it protects against
DeviationGuard is an **off-chain signal amplifier with on-chain attestation**.
It allows a monitoring keeper to record repeated oracle-vs-market deviations
on-chain, creating an auditable, immutable record that a guardian can act on.

The threat it is designed to detect:
1. **Oracle staleness** – Chainlink aggregators may freeze during network congestion,
   causing the oracle price to diverge from the live market.
2. **Exchange-rate adapter drift** – LST/LRT oracles (wstETH, weETH) use
   exchange-rate adapters.  If the staking protocol pauses withdrawals, the
   market price can deviate significantly from the oracle's rate-based price.
3. **Manipulation of DEX spot price** – A large flash-loan can move DEX spot
   before the oracle responds, temporarily widening the deviation.  The
   `consecutiveRequired` parameter mitigates this by requiring sustained deviation.

### What it does NOT protect against
1. **Oracle manipulation at the oracle layer** (Chainlink aggregators themselves
   are out of scope; this contract trusts what the keeper submits).
2. **Keeper compromise** – A malicious keeper can submit fabricated prices.
   Mitigated by: keepers can only submit data, not change thresholds or unflag.
   Multiple independent keepers can be granted `KEEPER_ROLE`.
3. **Guardian key compromise** – The guardian can unflag at will.  Use a
   multisig for the guardian role in any real deployment.
4. **Smart contract bugs** – This contract is a demonstration; it has not been
   audited.  Do not deploy it without a formal audit.
5. **Re-entrancy** – No external calls; not relevant here.
6. **Front-running** – A MEV searcher who knows a flag is about to be set gains
   no advantage from the contract state alone.

## Why automatic unflagging is avoided

A naïve circuit-breaker might auto-clear an alert once the deviation drops below
the threshold.  This is dangerous during stress:

- In a depeg event, DEX liquidity is thin.  One large buy can temporarily restore
  the market price, making the deviation look resolved, while the underlying
  peg stress persists.
- An automated system that auto-clears would restart lending or remove risk
  parameters precisely when the protocol is most exposed.

**Requiring a guardian action** ensures that a human reviews the context—
whether the deviation resolved organically, was masked by a temporary price
bounce, or was caused by oracle lag—before clearing the alert.

## Role assignment recommendations

| Role | Assignment |
|------|-----------|
| `DEFAULT_ADMIN_ROLE` | 3-of-5 multisig (DAO treasury or risk committee) |
| `GUARDIAN_ROLE` | 2-of-3 multisig (risk team on-call) |
| `KEEPER_ROLE` | Automated off-chain monitoring bots (multiple, independent) |

## Bounds on thresholds

Thresholds are bounded to `[10, 10_000]` bps (0.1% to 100%) to prevent:
- Setting threshold to 0 (flags every report).
- Setting threshold to > 100% (never fires even on full depeg).

These bounds are encoded as compile-time constants and cannot be changed.

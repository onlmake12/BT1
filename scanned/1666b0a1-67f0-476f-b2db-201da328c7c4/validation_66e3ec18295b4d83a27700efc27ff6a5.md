### Title
Incorrect Inline Comment Value for `MEDIUM_TARGET` Constant — (`File: util/fee-estimator/src/constants.rs`)

---

### Summary

The inline comment for `MEDIUM_TARGET` in the fee estimator constants file states the constant evaluates to `42`, but due to Rust integer division the actual runtime value is `21`. This is a direct analog to the UMA `SlashingLibrary` bug: an incorrect mathematical derivation documented in an inline comment that misrepresents the true value of a protocol-facing constant.

---

### Finding Description

In `util/fee-estimator/src/constants.rs`, the constant chain is:

```
MAX_BLOCK_INTERVAL  = 48   (spec/src/consensus.rs)
MIN_BLOCK_INTERVAL  = 8    (spec/src/consensus.rs)
AVG_BLOCK_INTERVAL  = (48 + 8) / 2          = 28
MAX_TARGET          = (60 * 60) / 28         = 128   (integer division: 3600/28 = 128.57 → 128)
DEFAULT_TARGET      = MAX_TARGET             = 128
LOW_TARGET          = DEFAULT_TARGET / 2     = 64
MEDIUM_TARGET       = LOW_TARGET / 3         = 21    (integer division: 64/3 = 21.33 → 21)
```

The comment on line 22 reads:

```rust
/// Target blocks for medium priority (about 10 minutes, 42).
pub const MEDIUM_TARGET: BlockNumber = LOW_TARGET / 3;
```

The parenthetical `42` is wrong. `LOW_TARGET / 3 = 64 / 3 = 21` in Rust (truncating integer division). The value `42` would only be correct if the expression were `DEFAULT_TARGET / 3 = 128 / 3 = 42` — a different formula entirely. The "about 10 minutes" description is accurate for the real value (`21 × 28 s = 588 s ≈ 9.8 min`), but the numeric annotation is off by a factor of 2. [1](#0-0) 

`MEDIUM_TARGET` is consumed directly by `FeeEstimator::target_blocks_for_estimate_mode` for `EstimateMode::MediumPriority`: [2](#0-1) 

which is the backend for the `estimate_fee_rate` RPC endpoint exposed to all RPC callers. [3](#0-2) 

---

### Impact Explanation

The runtime behavior of the fee estimator is correct — it uses `21` blocks as the medium-priority target. However, any developer, integrator, or operator who reads the comment and takes the annotated value `42` at face value will:

- Believe the medium-priority window is ~20 minutes (42 × 28 s = 1176 s) rather than the actual ~10 minutes.
- If they hardcode `42` in downstream tooling, scripts, or documentation instead of referencing the constant, their fee-estimation logic will target twice as many blocks, producing systematically lower fee-rate recommendations for medium-priority transactions.
- Incorrect fee rates derived from a wrong target block count can cause transactions to be under-priced and stuck in the mempool, or over-priced and wasteful, depending on the direction of the error.

---

### Likelihood Explanation

The `MEDIUM_TARGET` constant is `pub` and is part of the public API of the `ckb-fee-estimator` crate. Developers building wallets, exchanges, or tooling on top of CKB are the natural consumers of this constant and its documentation. The discrepancy is subtle (the "about 10 minutes" text is correct, only the numeric annotation is wrong), making it easy to miss during code review. The probability that an integrator reads the comment and trusts the annotated `42` rather than evaluating the expression is non-trivial.

---

### Recommendation

Correct the inline comment to reflect the actual computed value:

```rust
/// Target blocks for medium priority (about 10 minutes, 21).
pub const MEDIUM_TARGET: BlockNumber = LOW_TARGET / 3;
```

Additionally, consider adding a compile-time assertion to guard against future drift:

```rust
const _: () = assert!(MEDIUM_TARGET == 21, "MEDIUM_TARGET comment is out of sync");
```

This pattern would have caught the discrepancy immediately.

---

### Proof of Concept

Evaluate the constant chain at compile time (all values are `const`):

```
MAX_BLOCK_INTERVAL  = 48          // spec/src/consensus.rs line 65
MIN_BLOCK_INTERVAL  = 8           // spec/src/consensus.rs line 67
AVG_BLOCK_INTERVAL  = (48+8)/2   = 28
MAX_TARGET          = 3600/28     = 128   (Rust u64 truncation)
DEFAULT_TARGET      = 128
LOW_TARGET          = 128/2       = 64
MEDIUM_TARGET       = 64/3        = 21    (Rust u64 truncation, NOT 42)
```

The comment annotation `42` corresponds to `DEFAULT_TARGET / 3 = 128 / 3 = 42`, which is the wrong base constant. The actual expression `LOW_TARGET / 3` yields `21`. [4](#0-3) [3](#0-2)

### Citations

**File:** util/fee-estimator/src/constants.rs (L6-25)
```rust
/// Average block interval (28).
pub(crate) const AVG_BLOCK_INTERVAL: u64 = (MAX_BLOCK_INTERVAL + MIN_BLOCK_INTERVAL) / 2;

/// Max target blocks, about 1 hour (128).
pub(crate) const MAX_TARGET: BlockNumber = (60 * 60) / AVG_BLOCK_INTERVAL;
/// Min target blocks, in next block (5).
/// NOTE After tests, 3 blocks are too strict; so to adjust larger: 5.
pub(crate) const MIN_TARGET: BlockNumber = (TX_PROPOSAL_WINDOW.closest() + 1) + 2;

/// Lowest fee rate.
pub(crate) const LOWEST_FEE_RATE: FeeRate = FeeRate::from_u64(1000);

/// Target blocks for no priority (lowest priority, about 1 hour, 128).
pub const DEFAULT_TARGET: BlockNumber = MAX_TARGET;
/// Target blocks for low priority (about 30 minutes, 64).
pub const LOW_TARGET: BlockNumber = DEFAULT_TARGET / 2;
/// Target blocks for medium priority (about 10 minutes, 42).
pub const MEDIUM_TARGET: BlockNumber = LOW_TARGET / 3;
/// Target blocks for high priority (3).
pub const HIGH_TARGET: BlockNumber = MIN_TARGET;
```

**File:** util/fee-estimator/src/estimator/mod.rs (L41-48)
```rust
    pub const fn target_blocks_for_estimate_mode(estimate_mode: EstimateMode) -> BlockNumber {
        match estimate_mode {
            EstimateMode::NoPriority => constants::DEFAULT_TARGET,
            EstimateMode::LowPriority => constants::LOW_TARGET,
            EstimateMode::MediumPriority => constants::MEDIUM_TARGET,
            EstimateMode::HighPriority => constants::HIGH_TARGET,
        }
    }
```

**File:** spec/src/consensus.rs (L64-67)
```rust
/// max block interval, 48 seconds
pub const MAX_BLOCK_INTERVAL: u64 = 48;
/// min block interval, 8 seconds
pub const MIN_BLOCK_INTERVAL: u64 = 8;
```

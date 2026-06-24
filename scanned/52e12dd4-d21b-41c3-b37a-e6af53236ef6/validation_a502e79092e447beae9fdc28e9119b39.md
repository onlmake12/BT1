Audit Report

## Title
Floor Division in `threshold_number` Lowers Softfork Lock-In Threshold Below Configured Ratio — (`File: spec/src/versionbits/mod.rs`)

## Summary
The `threshold_number` helper at `spec/src/versionbits/mod.rs:475–479` uses integer floor division to compute the minimum signaling block count for softfork lock-in. Because CKB epoch lengths are variable, `total × numer` is frequently not divisible by `denom`, causing the computed threshold to be strictly lower than the configured ratio. A miner coalition controlling fewer blocks than the true configured threshold can satisfy the lock-in condition, causing a softfork to activate with less than the intended miner supermajority.

## Finding Description
The `Deployment` struct documents `threshold` as "the minimum ratio of block per period, which indicate the locked_in of the softfork during the period" (`spec/src/versionbits/mod.rs:138–140`). The correct integer formulation of `count/total >= numer/denom` requires `count >= ceil(total × numer / denom)`. The implementation computes:

```rust
// spec/src/versionbits/mod.rs:475–479
fn threshold_number(length: u64, threshold: Ratio) -> Option<u64> {
    length
        .checked_mul(threshold.numer())
        .and_then(|ret| ret.checked_div(threshold.denom()))
}
```

`checked_div` is integer floor division. The result is used at line 342–344:

```rust
let threshold_number = threshold_number(total, self.threshold())?;
if count >= threshold_number {
    next_state = ThresholdState::LockedIn;
}
```

`total` is accumulated across `period` epochs of variable length (`spec/src/versionbits/mod.rs:326–340`). Whenever `total × numer mod denom ≠ 0`, `floor < ceil`, so the effective activation threshold is strictly lower than `numer/denom`. No guard or compensating check exists anywhere in the state machine.

## Impact Explanation
This maps to **consensus deviation**: a softfork can transition to `LockedIn` and then `Active` with less than the intended miner supermajority. Once `Active`, the new consensus rules are enforced on all nodes. On mainnet (`LC_MAINNET_ACTIVATION_THRESHOLD = Ratio::new(8, 10)`, `spec/src/consensus.rs:99`) with typical epoch sizes (~1800 blocks, `total ≈ 3600`), the maximum discrepancy is 1 block (~0.028%), which is negligible. On testnet (`TESTNET_ACTIVATION_THRESHOLD = Ratio::new(3, 4)`, `spec/src/consensus.rs:101`) or any custom chain with small epoch sizes, the discrepancy can reach several percentage points (e.g., 60% effective vs. 75% configured for `total=5`). The severity is **low-to-medium**: the root cause is real and systematic, but the mainnet impact is bounded to ≤1 block per evaluation window, making practical exploitation on mainnet negligible. Testnet and custom chains with small epoch sizes are more meaningfully affected.

## Likelihood Explanation
The condition `total × numer mod denom ≠ 0` is the common case for variable-length epochs. No special privileges are required beyond the standard miner capability of setting version bits in produced blocks. The attacker needs only to control a fraction of hashrate between `floor(total × numer / denom) / total` and `numer / denom` during the signaling window — a narrow but structurally always-present gap whenever epoch lengths are not exact multiples of `denom`. On mainnet the gap is ≤1 block and thus practically unexploitable. On testnet or custom chains the gap can be several blocks and is reachable by any miner.

## Recommendation
Replace floor division with ceiling division in `threshold_number`:

```rust
// Option A: ceiling division
fn threshold_number(length: u64, threshold: Ratio) -> Option<u64> {
    length
        .checked_mul(threshold.numer())
        .map(|ret| (ret + threshold.denom() - 1) / threshold.denom())
}
```

Or eliminate rounding entirely at the call site:

```rust
// Option B: cross-multiply, no division
if count * self.threshold().denom() >= total * self.threshold().numer() {
    next_state = ThresholdState::LockedIn;
}
```

Option B is exact and directly expresses the ratio comparison `count/total >= numer/denom`.

## Proof of Concept
Given `threshold = Ratio::new(3, 4)` (testnet) and a signaling window where `total = 5`:

1. `threshold_number(5, Ratio::new(3, 4))` → `floor(5 × 3 / 4)` = `floor(3.75)` = **3**.
2. `count >= 3` passes with only 3 out of 5 blocks signaling (60%).
3. Correct ceiling check: `ceil(3.75)` = 4, requiring 4/5 blocks (80%).
4. A miner controlling 60% of blocks in that window — 15 percentage points below the 75% threshold — successfully locks in the softfork.

A unit test can reproduce this directly by constructing a `MockChain` with epoch length 5, `period=1`, `threshold=Ratio::new(3,4)`, signaling exactly 3 blocks, and asserting `ThresholdState::LockedIn` is reached — which it will be under the current floor-division code but should not be under a correct ceiling-division implementation.
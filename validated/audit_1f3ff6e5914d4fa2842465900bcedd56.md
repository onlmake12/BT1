### Title
Softfork Activation Threshold Bypassed via Integer Division Truncation in `threshold_number` - (File: `spec/src/versionbits/mod.rs`)

### Summary
The `threshold_number` function uses integer floor division to compute the minimum number of signaling blocks required to lock in a softfork. Because the result is truncated downward, a signaling ratio strictly below the configured threshold can satisfy the `count >= threshold_number` check, allowing a softfork to lock in with fewer miner signals than the protocol intends.

### Finding Description
The function `threshold_number` computes the minimum block count needed to meet the activation threshold:

```rust
fn threshold_number(length: u64, threshold: Ratio) -> Option<u64> {
    length
        .checked_mul(threshold.numer())
        .and_then(|ret| ret.checked_div(threshold.denom()))
}
``` [1](#0-0) 

This computes `floor(length × numer / denom)`. When `length × numer` is not evenly divisible by `denom`, the result is rounded **down**, producing a threshold that is strictly less than the true fractional requirement.

The result is then used in the lock-in check:

```rust
let threshold_number = threshold_number(total, self.threshold())?;
if count >= threshold_number {
    next_state = ThresholdState::LockedIn;
``` [2](#0-1) 

The mainnet activation threshold is `Ratio::new(8, 10)` (80%) and the testnet threshold is `Ratio::new(3, 4)` (75%): [3](#0-2) 

**Concrete example (mainnet, 80% threshold):**
- Suppose `total = 1001` blocks across the signaling period
- `threshold_number = floor(1001 × 8 / 10) = floor(800.8) = 800`
- 800 out of 1001 blocks = **79.92%** — strictly below the 80% threshold
- The check `800 >= 800` passes → `ThresholdState::LockedIn`

**Concrete example (testnet, 75% threshold):**
- Suppose `total = 1001` blocks
- `threshold_number = floor(1001 × 3 / 4) = floor(750.75) = 750`
- 750 out of 1001 = **74.93%** — strictly below the 75% threshold
- The check `750 >= 750` passes → `ThresholdState::LockedIn`

CKB epoch lengths are variable (between `MIN_EPOCH_LENGTH = 300` and `MAX_EPOCH_LENGTH = 1800` blocks), and the signaling period spans multiple epochs: [4](#0-3) 

The `total` block count across a period is the sum of variable-length epochs, making it very common for `total × numer` to not be divisible by `denom`.

### Impact Explanation
A softfork can be locked in with a miner signaling ratio strictly below the configured activation threshold. For the mainnet 80% threshold, the maximum bypass is up to `(denom - 1) / denom = 0.9` blocks below the true threshold — meaning as few as `ceil(total × 0.8) - 1` signaling blocks suffice. This is a consensus-level deviation: the protocol's stated governance rule (e.g., "80% of miners must signal") is not enforced exactly, allowing a softfork to activate with less miner consensus than intended.

**Impact: Medium** — The bypass margin is small (at most `denom - 1` blocks short of the true threshold), but it is a systematic, deterministic deviation from the protocol's stated activation rule, affecting every softfork deployment evaluated under `ThresholdState::Started`.

### Likelihood Explanation
**Likelihood: Medium** — CKB epoch lengths are dynamically adjusted and are rarely round multiples of the threshold denominator (10 for mainnet, 4 for testnet). Since `total` is the sum of multiple variable-length epochs, `total × numer mod denom ≠ 0` is the common case. Any miner coalition that can signal near (but not quite at) the true threshold benefits from this truncation.

### Recommendation
Replace floor division with ceiling division in `threshold_number`, or eliminate division entirely by restructuring the comparison:

**Option 1 — Ceiling division:**
```rust
fn threshold_number(length: u64, threshold: Ratio) -> Option<u64> {
    length
        .checked_mul(threshold.numer())
        .and_then(|ret| ret.checked_add(threshold.denom() - 1))
        .and_then(|ret| ret.checked_div(threshold.denom()))
}
```

**Option 2 — Avoid division in the comparison entirely:**
```rust
// Instead of: count >= threshold_number(total, threshold)?
// Use:
if count.checked_mul(self.threshold().denom())? >= total.checked_mul(self.threshold().numer())? {
    next_state = ThresholdState::LockedIn;
}
```

Option 2 is the most precise and directly mirrors the second fix suggested in the external report.

### Proof of Concept
With mainnet threshold `Ratio::new(8, 10)` and a period whose total block count is `1001`:

```
threshold_number(1001, Ratio::new(8, 10))
  = floor(1001 * 8 / 10)
  = floor(8008 / 10)
  = floor(800.8)
  = 800

True 80% of 1001 = 800.8, requiring ceil(800.8) = 801 blocks to genuinely meet the threshold.

count = 800  →  800 >= 800  →  LockedIn   ✓ (bypassed: 800/1001 = 79.92% < 80%)
count = 801  →  801 >= 800  →  LockedIn   ✓ (legitimate)
```

A miner coalition controlling exactly 800 out of 1001 blocks (79.92%) in a signaling period would lock in a softfork that requires 80% support, bypassing the threshold by the floor truncation in `threshold_number`. [1](#0-0) [2](#0-1)

### Citations

**File:** spec/src/versionbits/mod.rs (L342-344)
```rust
                    let threshold_number = threshold_number(total, self.threshold())?;
                    if count >= threshold_number {
                        next_state = ThresholdState::LockedIn;
```

**File:** spec/src/versionbits/mod.rs (L475-479)
```rust
fn threshold_number(length: u64, threshold: Ratio) -> Option<u64> {
    length
        .checked_mul(threshold.numer())
        .and_then(|ret| ret.checked_div(threshold.denom()))
}
```

**File:** spec/src/consensus.rs (L77-78)
```rust
const MAX_EPOCH_LENGTH: u64 = DEFAULT_EPOCH_DURATION_TARGET / MIN_BLOCK_INTERVAL; // 1800
const MIN_EPOCH_LENGTH: u64 = DEFAULT_EPOCH_DURATION_TARGET / MAX_BLOCK_INTERVAL; // 300
```

**File:** spec/src/consensus.rs (L98-101)
```rust
/// The mainnet default activation_threshold
pub const LC_MAINNET_ACTIVATION_THRESHOLD: Ratio = Ratio::new(8, 10);
/// The testnet default activation_threshold
pub const TESTNET_ACTIVATION_THRESHOLD: Ratio = Ratio::new(3, 4);
```

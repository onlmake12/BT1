### Title
Genesis Hash Rate Truncates to Zero via Integer Division, Bypassing Difficulty Dampening Filter — (File: `spec/src/consensus.rs`)

---

### Summary

In `build_genesis_epoch_ext`, the `genesis_hash_rate` is computed using integer division where the numerator is always smaller than the denominator when using the minimum difficulty (`DIFF_TWO`). The result is always `0`, which is then stored as `previous_epoch_hash_rate` in the genesis epoch. When the first epoch ends, the dampening filter in `bounding_hash_rate` detects this zero and unconditionally bypasses the TAU-bounded dampening, allowing the difficulty to change by an unbounded factor at the first epoch boundary.

---

### Finding Description

In `spec/src/consensus.rs`, `build_genesis_epoch_ext` computes:

```rust
let genesis_hash_rate = compact_to_difficulty(compact_target)
    * (genesis_epoch_length + genesis_orphan_count)
    / epoch_duration_target;
``` [1](#0-0) 

With the default and mainnet constants:

- `compact_to_difficulty(DIFF_TWO)` = **2** (DIFF_TWO = `0x2080_0000` decodes to difficulty 2)
- `genesis_epoch_length` = **1000** [2](#0-1) 
- `genesis_orphan_count` = `1000 * 1 / 40` = **25** [3](#0-2) 
- `epoch_duration_target` = **14400** seconds [4](#0-3) 

Integer division: `2 * (1000 + 25) / 14400 = 2050 / 14400 = 0`.

So `genesis_hash_rate = 0` is stored as `previous_epoch_hash_rate` in the genesis `EpochExt`: [5](#0-4) 

When the first epoch ends and `next_epoch_ext` calls `bounding_hash_rate`, the function receives `last_epoch_previous_hash_rate = 0` and immediately returns the raw, unbounded hash rate:

```rust
fn bounding_hash_rate(...) -> U256 {
    if last_epoch_previous_hash_rate == U256::zero() {
        return last_epoch_hash_rate;  // dampening skipped entirely
    }
    let lower_bound = &last_epoch_previous_hash_rate / TAU;
    ...
    let upper_bound = &last_epoch_previous_hash_rate * TAU;
    ...
}
``` [6](#0-5) 

The TAU=2 dampening bound (which limits hash rate changes to ±2×) is never applied for the first epoch transition.

---

### Impact Explanation

The difficulty adjustment algorithm is designed to limit epoch-to-epoch difficulty changes to a factor of TAU (2×). Because `genesis_hash_rate` is always 0 due to integer division truncation, the dampening filter is unconditionally bypassed at the first epoch boundary. If the first epoch is mined significantly faster or slower than the 4-hour target (which is common since `DIFF_TWO` is the minimum difficulty and the chain starts with essentially no hash power requirement), the difficulty for epoch 2 can jump or drop by an arbitrarily large factor — far beyond the intended 2× bound. This directly affects the consensus-level difficulty adjustment mechanism and the predictability of block production rates.

The downstream calculation in `next_epoch_ext` does apply `cmp::max(..., U256::one())` to prevent a zero `adjusted_last_epoch_hash_rate`, but this does not restore the dampening bound — it only prevents a zero difficulty. [7](#0-6) 

---

### Likelihood Explanation

This affects **every** CKB chain (mainnet, testnet, and any custom chain) that uses `DIFF_TWO` as the genesis compact target, which is the default. The truncation is deterministic and unconditional — `genesis_hash_rate` is always 0 under these parameters. The first epoch boundary is reached by every chain, so the dampening bypass is guaranteed to occur exactly once per chain. No special attacker capability is required; any miner participating in the first epoch observes this behavior.

---

### Recommendation

Multiply the numerator by a sufficiently large scaling factor before dividing, then divide the result back, analogous to the original report's suggestion of increasing `MULTIPLIER`. For example:

```rust
const HASH_RATE_SCALE: u64 = 1_000_000;
let genesis_hash_rate = compact_to_difficulty(compact_target)
    * (genesis_epoch_length + genesis_orphan_count)
    * HASH_RATE_SCALE
    / epoch_duration_target;
// Store scaled value and divide by HASH_RATE_SCALE when comparing
```

Alternatively, use a `RationalU256` representation (already available in the codebase) to preserve precision throughout the hash rate calculation, consistent with how `orphan_rate_target` and `last_orphan_rate` are handled. [8](#0-7) 

---

### Proof of Concept

The truncation can be verified directly from the constants:

```rust
// From spec/src/consensus.rs
const GENESIS_EPOCH_LENGTH: u64 = 1_000;
const DEFAULT_ORPHAN_RATE_TARGET: (u32, u32) = (1, 40);
const DEFAULT_EPOCH_DURATION_TARGET: u64 = 4 * 60 * 60; // 14400

// compact_to_difficulty(DIFF_TWO) == 2 (verified from difficulty.rs)
let genesis_orphan_count = 1_000u64 * 1 / 40; // = 25
let genesis_hash_rate = 2u64 * (1_000 + 25) / 14_400;
assert_eq!(genesis_hash_rate, 0); // always zero — dampening bypassed
``` [9](#0-8) [10](#0-9)

### Citations

**File:** spec/src/consensus.rs (L59-80)
```rust
pub(crate) const GENESIS_EPOCH_LENGTH: u64 = 1_000;

// o_ideal = 1/40 = 2.5%
pub(crate) const DEFAULT_ORPHAN_RATE_TARGET: (u32, u32) = (1, 40);

/// max block interval, 48 seconds
pub const MAX_BLOCK_INTERVAL: u64 = 48;
/// min block interval, 8 seconds
pub const MIN_BLOCK_INTERVAL: u64 = 8;

/// cycles of a typical two-in-two-out tx.
pub const TWO_IN_TWO_OUT_CYCLES: Cycle = 3_500_000;
/// bytes of a typical two-in-two-out tx.
pub const TWO_IN_TWO_OUT_BYTES: u64 = 597;
/// count of two-in-two-out txs a block should capable to package.
const TWO_IN_TWO_OUT_COUNT: u64 = 1_000;
pub(crate) const DEFAULT_EPOCH_DURATION_TARGET: u64 = 4 * 60 * 60; // 4 hours, unit: second
const MILLISECONDS_IN_A_SECOND: u64 = 1000;
const MAX_EPOCH_LENGTH: u64 = DEFAULT_EPOCH_DURATION_TARGET / MIN_BLOCK_INTERVAL; // 1800
const MIN_EPOCH_LENGTH: u64 = DEFAULT_EPOCH_DURATION_TARGET / MAX_BLOCK_INTERVAL; // 300
pub(crate) const DEFAULT_PRIMARY_EPOCH_REWARD_HALVING_INTERVAL: EpochNumber =
    4 * 365 * 24 * 60 * 60 / DEFAULT_EPOCH_DURATION_TARGET; // every 4 years
```

**File:** spec/src/consensus.rs (L225-226)
```rust
    let genesis_orphan_count =
        genesis_epoch_length * genesis_orphan_rate.0 as u64 / genesis_orphan_rate.1 as u64;
```

**File:** spec/src/consensus.rs (L227-229)
```rust
    let genesis_hash_rate = compact_to_difficulty(compact_target)
        * (genesis_epoch_length + genesis_orphan_count)
        / epoch_duration_target;
```

**File:** spec/src/consensus.rs (L231-241)
```rust
    EpochExt::new_builder()
        .number(0)
        .base_block_reward(block_reward)
        .remainder_reward(remainder_reward)
        .previous_epoch_hash_rate(genesis_hash_rate)
        .last_block_hash_in_previous_epoch(Byte32::zero())
        .start_number(0)
        .length(genesis_epoch_length)
        .compact_target(compact_target)
        .build()
}
```

**File:** spec/src/consensus.rs (L774-794)
```rust
    // Apply the dampening filter on hash_rate estimation calculate
    fn bounding_hash_rate(
        &self,
        last_epoch_hash_rate: U256,
        last_epoch_previous_hash_rate: U256,
    ) -> U256 {
        if last_epoch_previous_hash_rate == U256::zero() {
            return last_epoch_hash_rate;
        }

        let lower_bound = &last_epoch_previous_hash_rate / TAU;
        if last_epoch_hash_rate < lower_bound {
            return lower_bound;
        }

        let upper_bound = &last_epoch_previous_hash_rate * TAU;
        if last_epoch_hash_rate > upper_bound {
            return upper_bound;
        }
        last_epoch_hash_rate
    }
```

**File:** spec/src/consensus.rs (L858-868)
```rust
                        let last_epoch_hash_rate = last_difficulty
                            * (epoch.length() + epoch_uncles_count)
                            / &last_epoch_duration;

                        let adjusted_last_epoch_hash_rate = cmp::max(
                            self.bounding_hash_rate(
                                last_epoch_hash_rate,
                                epoch.previous_epoch_hash_rate().to_owned(),
                            ),
                            U256::one(),
                        );
```

**File:** util/rational/src/lib.rs (L27-77)
```rust
impl RationalU256 {
    /// Creates a new ratio `numer / denom`.
    ///
    /// ## Panics
    ///
    /// Panics when `denom` is zero.
    #[inline]
    pub fn new(numer: U256, denom: U256) -> RationalU256 {
        if denom.is_zero() {
            panic!("denominator == 0");
        }
        let mut ret = RationalU256::new_raw(numer, denom);
        ret.reduce();
        ret
    }

    /// Creates a new ratio `numer / denom` without checking whether `denom` is zero.
    #[inline]
    pub const fn new_raw(numer: U256, denom: U256) -> RationalU256 {
        RationalU256 { numer, denom }
    }

    /// Creates a new ratio `t / 1`.
    #[inline]
    pub const fn from_u256(t: U256) -> RationalU256 {
        RationalU256::new_raw(t, U256::one())
    }

    /// Tells whether the numerator is zero.
    #[inline]
    pub fn is_zero(&self) -> bool {
        self.numer.is_zero()
    }

    /// Creates a new ratio `0 / 1`.
    #[inline]
    pub const fn zero() -> RationalU256 {
        RationalU256::new_raw(U256::zero(), U256::one())
    }

    /// Creates a new ratio `1 / 1`.
    #[inline]
    pub const fn one() -> RationalU256 {
        RationalU256::new_raw(U256::one(), U256::one())
    }

    /// Rounds down the ratio into an unsigned 256-bit integer.
    #[inline]
    pub fn into_u256(self) -> U256 {
        self.numer / self.denom
    }
```

**File:** util/types/src/utilities/difficulty.rs (L1-27)
```rust
use numext_fixed_uint::prelude::UintConvert;
use numext_fixed_uint::{U256, U512, u512};

/// The minimal difficulty that can be represented in the compact format.
pub const DIFF_TWO: u32 = 0x2080_0000;

const ONE: U256 = U256::one();
// ONE << 256
const HSPACE: U512 = u512!("0x10000000000000000000000000000000000000000000000000000000000000000");

fn target_to_difficulty(target: &U256) -> U256 {
    if target == &ONE {
        U256::max_value()
    } else {
        let (target, _): (U512, bool) = target.convert_into();
        (HSPACE / target).convert_into().0
    }
}

fn difficulty_to_target(difficulty: &U256) -> U256 {
    if difficulty == &ONE {
        U256::max_value()
    } else {
        let (difficulty, _): (U512, bool) = difficulty.convert_into();
        (HSPACE / difficulty).convert_into().0
    }
}
```

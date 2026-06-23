### Title
Phantom Overflow in `Capacity::safe_mul_ratio` Causes Spurious Block Reward Calculation Failure — (File: `util/occupied-capacity/core/src/units.rs`)

### Summary

`Capacity::safe_mul_ratio` computes `capacity * numer / denom` using `u64::checked_mul` for the intermediate product. When `capacity * numer` exceeds `u64::MAX`, the function returns `Err(Overflow)` even though the final result `capacity * numer / denom` would fit in `u64`. This is the direct Rust analog of the Solidity phantom-overflow bug: the intermediate value overflows the native integer width, but the final quotient is representable. Other DAO arithmetic in the same codebase correctly widens to `u128` for the intermediate step; `safe_mul_ratio` does not.

### Finding Description

`safe_mul_ratio` is defined as:

```rust
// util/occupied-capacity/core/src/units.rs  lines 149-155
pub fn safe_mul_ratio(self, ratio: Ratio) -> Result<Self> {
    self.0
        .checked_mul(ratio.numer())          // u64 × u64 → Option<u64>
        .and_then(|ret| ret.checked_div(ratio.denom()))
        .map(Capacity::shannons)
        .ok_or(Error::Overflow)
}
```

`Ratio` holds two `u64` fields (`numer`, `denom`). If `self.0 * ratio.numer() > u64::MAX`, `checked_mul` returns `None` and the function propagates `Err(Overflow)`, even when `(self.0 * ratio.numer()) / ratio.denom()` would be a valid `u64`.

By contrast, every other ratio-style multiplication in the DAO subsystem correctly promotes to `u128`:

```rust
// util/dao/src/lib.rs  lines 152-154
let withdraw_counted_capacity = u128::from(counted_capacity.as_u64())
    * u128::from(withdrawing_ar)
    / u128::from(deposit_ar);
```

```rust
// util/dao/src/lib.rs  lines 202-203
let reward128 = u128::from(target_g2.as_u64()) * u128::from(target_parent_u.as_u64())
    / u128::from(target_parent_c.as_u64());
```

`safe_mul_ratio` is the only ratio-multiply that stays in `u64`.

### Impact Explanation

`safe_mul_ratio` is called in two places inside `RewardCalculator`:

1. **`txs_fees()`** (`util/reward-calculator/src/lib.rs` line 149) — iterates every transaction fee in a block and calls `tx_fee.safe_mul_ratio(proposer_reward_ratio)`. A spurious `Err(Overflow)` here propagates through `block_reward_internal` and causes the entire block to be rejected by every node that processes it.

2. **`proposal_reward()`** (`util/reward-calculator/src/lib.rs` lines 233, 266) — same call for each fee in the proposal window. Same rejection consequence.

It is also called in `modified_occupied_capacity` (`util/dao/src/lib.rs` line 348) for the Satoshi gift cell, and in `genesis_dao_data_with_satoshi_gift` (`util/dao/utils/src/lib.rs` line 68) for genesis DAO field construction.

If the overflow fires during block processing, the block is rejected as invalid by all honest nodes, even though the block and its transactions are otherwise well-formed. This constitutes a consensus-level block rejection / potential chain split.

### Likelihood Explanation

On mainnet the `proposer_reward_ratio` is `Ratio::new(4, 10)`. The overflow threshold for a single transaction fee is `u64::MAX / 4 ≈ 4.6 × 10^18 shannons ≈ 46 billion CKB`. The current total CKB supply is ~33.6 billion CKB, so the threshold is not reachable today. However:

- The total supply grows via secondary issuance (~1.344 billion CKB/year); the threshold will be crossed in the distant future.
- Any chain spec that sets a larger `numer` (e.g., `Ratio::new(9, 10)`) lowers the threshold to `u64::MAX / 9 ≈ 2 billion CKB`, well within reach.
- The `satoshi_cell_occupied_ratio` path is reachable at genesis with sufficiently large cell capacity and numerator.

Likelihood is **low** for current mainnet parameters but the code is structurally incorrect and inconsistent with every other ratio calculation in the same codebase.

### Recommendation

Replace the `u64` intermediate with a `u128` promotion, matching the pattern used throughout `util/dao/src/lib.rs`:

```rust
pub fn safe_mul_ratio(self, ratio: Ratio) -> Result<Self> {
    let result = u128::from(self.0)
        .checked_mul(u128::from(ratio.numer()))
        .and_then(|n| n.checked_div(u128::from(ratio.denom())))
        .and_then(|n| u64::try_from(n).ok())
        .map(Capacity::shannons)
        .ok_or(Error::Overflow);
    result
}
```

This eliminates phantom overflow while still correctly detecting genuine overflow (result > `u64::MAX`).

### Proof of Concept

```
capacity  = u64::MAX / 4 + 1  = 4_611_686_018_427_387_905  shannons
ratio     = Ratio::new(4, 10)  (mainnet proposer_reward_ratio)

current code:
  checked_mul(4) → None  (4_611_686_018_427_387_905 * 4 > u64::MAX)
  → Err(Overflow)   ← spurious rejection

correct result:
  u128: 4_611_686_018_427_387_905 * 4 = 18_446_744_073_709_551_620
  / 10 = 1_844_674_407_370_955_162   ← fits in u64, valid capacity
```

Any block whose cellbase reward calculation calls `safe_mul_ratio` with a `tx_fee` at or above this threshold would be rejected by every node, even though the block is otherwise valid. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4) [6](#0-5) [7](#0-6)

### Citations

**File:** util/occupied-capacity/core/src/units.rs (L148-155)
```rust
    /// Multiplies self with a ratio and checks overflow error.
    pub fn safe_mul_ratio(self, ratio: Ratio) -> Result<Self> {
        self.0
            .checked_mul(ratio.numer())
            .and_then(|ret| ret.checked_div(ratio.denom()))
            .map(Capacity::shannons)
            .ok_or(Error::Overflow)
    }
```

**File:** util/dao/src/lib.rs (L152-154)
```rust
        let withdraw_counted_capacity = u128::from(counted_capacity.as_u64())
            * u128::from(withdrawing_ar)
            / u128::from(deposit_ar);
```

**File:** util/dao/src/lib.rs (L202-204)
```rust
        let reward128 = u128::from(target_g2.as_u64()) * u128::from(target_parent_u.as_u64())
            / u128::from(target_parent_c.as_u64());
        let reward = u64::try_from(reward128).map_err(|_| DaoError::Overflow)?;
```

**File:** util/dao/src/lib.rs (L338-349)
```rust
pub fn modified_occupied_capacity(
    cell_meta: &CellMeta,
    consensus: &Consensus,
) -> CapacityResult<Capacity> {
    if let Some(tx_info) = &cell_meta.transaction_info
        && tx_info.is_genesis()
        && tx_info.is_cellbase()
        && cell_meta.cell_output.lock().args().raw_data() == consensus.satoshi_pubkey_hash.0[..]
    {
        return Into::<Capacity>::into(cell_meta.cell_output.capacity())
            .safe_mul_ratio(consensus.satoshi_cell_occupied_ratio);
    }
```

**File:** util/reward-calculator/src/lib.rs (L144-156)
```rust
        target_ext
            .txs_fees
            .iter()
            .try_fold(Capacity::zero(), |acc, tx_fee| {
                tx_fee
                    .safe_mul_ratio(consensus.proposer_reward_ratio())
                    .and_then(|proposer| {
                        tx_fee
                            .safe_sub(proposer)
                            .and_then(|miner| acc.safe_add(miner))
                    })
            })
    }
```

**File:** util/reward-calculator/src/lib.rs (L226-235)
```rust
        if has_committed {
            for (id, tx_fee) in committed_idx
                .into_iter()
                .zip(txs_fees_proc(&index.hash()).iter())
            {
                // target block is the earliest block with effective proposals for the parent block
                if target_proposals.remove(&id) {
                    reward = reward.safe_add(tx_fee.safe_mul_ratio(proposer_ratio)?)?;
                }
            }
```

**File:** util/reward-calculator/src/lib.rs (L260-268)
```rust
            if has_committed {
                for (id, tx_fee) in committed_idx
                    .into_iter()
                    .zip(txs_fees_proc(&index.hash()).iter())
                {
                    if target_proposals.remove(&id) && !proposed.contains(&id) {
                        reward = reward.safe_add(tx_fee.safe_mul_ratio(proposer_ratio)?)?;
                    }
                }
```

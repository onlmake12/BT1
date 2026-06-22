### Title
Hardcoded `DEFAULT_BYTES_PER_CYCLES` Constant Decoupled from Consensus Parameters Causes Stale Weight Calculations — (File: `util/types/src/core/tx_pool.rs`)

---

### Summary

`DEFAULT_BYTES_PER_CYCLES` is baked in as a hardcoded floating-point literal rather than being derived at runtime from the actual consensus parameters `MAX_BLOCK_BYTES` and `MAX_BLOCK_CYCLES`. If either consensus parameter is updated via a hard fork, the weight-conversion constant silently diverges, corrupting fee-rate calculations, tx-pool admission, and miner block assembly for every transaction submitted thereafter.

---

### Finding Description

In `util/types/src/core/tx_pool.rs`, the constant that converts VM cycles to byte-weight is defined as a literal:

```rust
/// Equal to MAX_BLOCK_BYTES / MAX_BLOCK_CYCLES, see ckb-chain-spec.
pub const DEFAULT_BYTES_PER_CYCLES: f64 = 0.000_170_571_4_f64;
``` [1](#0-0) 

The comment acknowledges it is supposed to equal `MAX_BLOCK_BYTES / MAX_BLOCK_CYCLES`, but those values are defined separately in the chain spec:

```rust
pub const MAX_BLOCK_BYTES: u64 = TWO_IN_TWO_OUT_BYTES * TWO_IN_TWO_OUT_COUNT; // 597_000
pub(crate) const MAX_BLOCK_CYCLES: u64 = TWO_IN_TWO_OUT_CYCLES * TWO_IN_TWO_OUT_COUNT; // 3_500_000_000
``` [2](#0-1) 

`DEFAULT_BYTES_PER_CYCLES` is consumed directly in `get_transaction_weight()`:

```rust
pub fn get_transaction_weight(tx_size: usize, cycles: u64) -> u64 {
    std::cmp::max(
        tx_size as u64,
        (cycles as f64 * DEFAULT_BYTES_PER_CYCLES) as u64,
    )
}
``` [3](#0-2) 

`get_transaction_weight` is called throughout the tx-pool for fee-rate scoring, eviction ordering, and ancestor-weight accounting: [4](#0-3) 

The tx-pool also enforces a separate hardcoded size ceiling:

```rust
pub const TRANSACTION_SIZE_LIMIT: u64 = 512 * 1_000;
``` [5](#0-4) 

This limit is checked unconditionally in `non_contextual_verify`: [6](#0-5) 

Neither constant is recomputed when the node loads its consensus object; both are compile-time literals.

---

### Impact Explanation

If `MAX_BLOCK_BYTES` or `MAX_BLOCK_CYCLES` is adjusted by a hard fork (e.g., to accommodate more complex scripts or larger blocks):

1. **Fee-rate mispricing** — `get_transaction_weight` returns values calibrated to the old ratio. High-cycle transactions are under- or over-weighted, so their effective fee rate is computed incorrectly. Transactions that genuinely pay the minimum fee rate may be rejected; transactions that underpay may be admitted.
2. **Block-assembly distortion** — Miners use the same weight function to select transactions. A stale `DEFAULT_BYTES_PER_CYCLES` causes the greedy knapsack to produce suboptimal or invalid block templates relative to the new consensus limits.
3. **`TRANSACTION_SIZE_LIMIT` mismatch** — If `MAX_BLOCK_BYTES` shrinks below 512 KB, the pool accepts transactions that can never be committed; if it grows, the pool unnecessarily rejects valid large transactions.
4. **Fee-estimation corruption** — The `weight_units_flow` and `FeeRateCollector` estimators both call `get_transaction_weight`, so fee estimates returned to RPC callers become systematically wrong.

All of these effects are reachable by any unprivileged transaction sender or RPC caller without any special access.

---

### Likelihood Explanation

The trigger is a consensus upgrade that changes `MAX_BLOCK_BYTES` or `MAX_BLOCK_CYCLES` without simultaneously updating the hardcoded literal. CKB has already adjusted `TWO_IN_TWO_OUT_COUNT` and related parameters in past releases (e.g., the changelog records `TWO_IN_TWO_OUT_COUNT 3875 -> 1600`). The constant is not computed from the live consensus object, so a developer updating the spec constants without noticing the detached literal in `tx_pool.rs` is a realistic oversight. The probability is low in any single upgrade but non-zero across the protocol's lifetime, and the consequence is silent, widespread mispricing rather than a loud crash.

---

### Recommendation

Replace the hardcoded literal with a runtime derivation from the consensus object wherever `get_transaction_weight` is called, or at minimum compute it once from the canonical constants:

```rust
// Derived, not hardcoded:
pub fn bytes_per_cycle() -> f64 {
    MAX_BLOCK_BYTES as f64 / MAX_BLOCK_CYCLES as f64
}

pub fn get_transaction_weight(tx_size: usize, cycles: u64) -> u64 {
    std::cmp::max(
        tx_size as u64,
        (cycles as f64 * bytes_per_cycle()) as u64,
    )
}
```

Similarly, derive `TRANSACTION_SIZE_LIMIT` from `MAX_BLOCK_BYTES` (e.g., `MAX_BLOCK_BYTES * 512 / 597`) rather than hardcoding `512 * 1_000`.

---

### Proof of Concept

1. Current state: `MAX_BLOCK_BYTES = 597_000`, `MAX_BLOCK_CYCLES = 3_500_000_000`, `DEFAULT_BYTES_PER_CYCLES ≈ 0.000_170_571_4` — consistent.
2. Suppose a hard fork doubles `MAX_BLOCK_CYCLES` to `7_000_000_000` (to allow heavier scripts) while `MAX_BLOCK_BYTES` stays at `597_000`. The correct ratio becomes `≈ 0.000_085_285_7`.
3. `DEFAULT_BYTES_PER_CYCLES` remains `0.000_170_571_4` — twice the correct value.
4. A transaction with `cycles = 10_000_000` and `tx_size = 200` bytes now gets `weight = max(200, 10_000_000 × 0.000_170_571_4) ≈ 1_706` instead of the correct `≈ 853`.
5. Its fee rate is halved in the pool's view. A sender paying exactly the minimum fee rate for the new consensus is rejected by the pool even though the transaction is valid on-chain.
6. Conversely, a sender who knows the stale constant can craft a transaction whose pool-computed weight is artificially low, gaining priority over legitimately higher-fee transactions.

### Citations

**File:** util/types/src/core/tx_pool.rs (L276-279)
```rust
/// Equal to MAX_BLOCK_BYTES / MAX_BLOCK_CYCLES, see ckb-chain-spec.
/// The precision is set so that the difference between MAX_BLOCK_CYCLES * DEFAULT_BYTES_PER_CYCLES
/// and MAX_BLOCK_BYTES is less than 1.
pub const DEFAULT_BYTES_PER_CYCLES: f64 = 0.000_170_571_4_f64;
```

**File:** util/types/src/core/tx_pool.rs (L298-303)
```rust
pub fn get_transaction_weight(tx_size: usize, cycles: u64) -> u64 {
    std::cmp::max(
        tx_size as u64,
        (cycles as f64 * DEFAULT_BYTES_PER_CYCLES) as u64,
    )
}
```

**File:** util/types/src/core/tx_pool.rs (L309-309)
```rust
pub const TRANSACTION_SIZE_LIMIT: u64 = 512 * 1_000;
```

**File:** spec/src/consensus.rs (L83-84)
```rust
pub const MAX_BLOCK_BYTES: u64 = TWO_IN_TWO_OUT_BYTES * TWO_IN_TWO_OUT_COUNT;
pub(crate) const MAX_BLOCK_CYCLES: u64 = TWO_IN_TWO_OUT_CYCLES * TWO_IN_TWO_OUT_COUNT;
```

**File:** tx-pool/src/component/entry.rs (L221-231)
```rust
impl From<&TxEntry> for AncestorsScoreSortKey {
    fn from(entry: &TxEntry) -> Self {
        let weight = get_transaction_weight(entry.size, entry.cycles);
        let ancestors_weight = get_transaction_weight(entry.ancestors_size, entry.ancestors_cycles);
        AncestorsScoreSortKey {
            fee: entry.fee,
            weight,
            ancestors_fee: entry.ancestors_fee,
            ancestors_weight,
        }
    }
```

**File:** tx-pool/src/util.rs (L67-73)
```rust
    let tx_size = tx.data().serialized_size_in_block() as u64;
    if tx_size > TRANSACTION_SIZE_LIMIT {
        return Err(Reject::ExceededTransactionSizeLimit(
            tx_size,
            TRANSACTION_SIZE_LIMIT,
        ));
    }
```

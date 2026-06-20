### Title
Minimum Fee Rate Admission Check Uses Size-Only Weight While Pool Fee Rate Uses Full Weight, Allowing Compute-Heavy Transactions to Bypass the Min-Fee Filter - (File: `tx-pool/src/util.rs`)

### Summary

The tx-pool admission check (`check_tx_fee`) enforces the minimum fee rate using only the transaction's serialized byte size as the weight denominator. However, once admitted, the transaction's actual stored fee rate is computed using `get_transaction_weight(size, cycles) = max(size, cycles * DEFAULT_BYTES_PER_CYCLES)`. For compute-heavy transactions where `cycles * DEFAULT_BYTES_PER_CYCLES > size`, the two weights diverge, allowing a transaction sender to pass the admission gate with a fee that satisfies `fee >= min_fee_rate * size` but whose true effective fee rate is below `min_fee_rate`.

### Finding Description

In `tx-pool/src/util.rs`, `check_tx_fee` computes the minimum required fee as:

```rust
let min_fee = tx_pool.config.min_fee_rate.fee(tx_size as u64);
```

The code comment explicitly acknowledges the approximation: *"Theoretically we cannot use size as weight directly to calculate fee_rate, here min fee rate is used as a cheap check, so we will use size to calculate fee_rate directly."* [1](#0-0) 

After admission, the transaction is stored as a `TxEntry`. Its actual fee rate is computed in `tx-pool/src/component/entry.rs`:

```rust
pub fn fee_rate(&self) -> FeeRate {
    let weight = get_transaction_weight(self.size, self.cycles);
    FeeRate::calculate(self.fee, weight)
}
``` [2](#0-1) 

`get_transaction_weight` is defined as:

```rust
pub fn get_transaction_weight(tx_size: usize, cycles: u64) -> u64 {
    std::cmp::max(
        tx_size as u64,
        (cycles as f64 * DEFAULT_BYTES_PER_CYCLES) as u64,
    )
}
``` [3](#0-2) 

For a compute-heavy transaction where `cycles * DEFAULT_BYTES_PER_CYCLES > tx_size`:

- **Admission check weight**: `tx_size` (size only)
- **Actual pool fee rate weight**: `cycles * DEFAULT_BYTES_PER_CYCLES` (larger)

A sender who pays exactly `fee = min_fee_rate * tx_size` passes the admission gate, but the transaction's effective fee rate inside the pool is `fee / (cycles * DEFAULT_BYTES_PER_CYCLES) < min_fee_rate`. The same inconsistency applies to pool ordering (`AncestorsScoreSortKey`), eviction (`EvictKey`), and fee estimation, all of which use `get_transaction_weight`. [4](#0-3) 

The `check_tx_fee` function is called in both the normal and RBF resolution paths inside `pre_check`, and also in `readd_detached_tx` during reorg handling — all with the same size-only weight. [5](#0-4) 

### Impact Explanation

An unprivileged transaction sender (local RPC caller or P2P relay peer) can craft a compute-heavy transaction — one with a large cycle count but small serialized byte size — and pay a fee equal to `min_fee_rate * tx_size`. This transaction:

1. Passes `check_tx_fee` because `fee >= min_fee_rate * tx_size`.
2. Enters the pool with an effective fee rate below `min_fee_rate` (the node's stated spam-prevention threshold).
3. Consumes pool memory and CPU verification resources at below-minimum cost.
4. May be relayed to peers, propagating the below-minimum-fee-rate transaction across the network.

The pool's eviction logic (`limit_size`) uses the weight-based fee rate, so these transactions are eventually evicted when the pool fills — but only after consuming resources. The inconsistency allows sustained pool resource consumption at a cost lower than the operator-configured minimum.

### Likelihood Explanation

Any unprivileged transaction sender can reach this path via `send_transaction` RPC or P2P relay. Crafting a compute-heavy transaction with a small serialized size is straightforward: use a script that performs many VM cycles (e.g., a tight loop) while keeping the transaction's witness and output data small. The attacker pays only `min_fee_rate * tx_size` shannons, which for a small transaction is negligible, while the actual computational burden on the node is proportional to cycles.

### Recommendation

Replace the size-only weight in `check_tx_fee` with the full `get_transaction_weight` calculation. Since cycles are not yet known at the pre-check stage (they are computed during `verify_rtx`), the check should either:

1. Use the declared cycles (available for remote transactions via `declared_cycles`) as an upper bound for the weight in the admission check, or
2. Re-validate the fee rate after `verify_rtx` returns the actual cycles, rejecting the transaction if `fee < min_fee_rate.fee(get_transaction_weight(tx_size, verified.cycles))`.

This aligns the admission gate with the fee rate metric used for pool ordering, eviction, and fee estimation.

### Proof of Concept

```
// Attacker constructs a transaction with:
//   tx_size = 200 bytes (small serialized size)
//   cycles  = 10_000_000 (compute-heavy script)
//   DEFAULT_BYTES_PER_CYCLES = 0.000_170_571_4
//   weight  = max(200, 10_000_000 * 0.000_170_571_4) = max(200, 1705) = 1705
//
// min_fee_rate = 1000 shannons/KW (default)
//
// check_tx_fee requires: fee >= min_fee_rate.fee(tx_size) = 1000 * 200 / 1000 = 200 shannons
// Attacker pays: fee = 200 shannons  → PASSES admission
//
// Actual fee rate in pool: FeeRate::calculate(200, 1705) = 200 * 1000 / 1705 = 117 shannons/KW
// This is below min_fee_rate (1000 shannons/KW).
//
// The transaction enters the pool with effective fee rate 117 shannons/KW,
// far below the operator's configured minimum of 1000 shannons/KW.
``` [6](#0-5) [7](#0-6)

### Citations

**File:** tx-pool/src/util.rs (L28-54)
```rust
pub(crate) fn check_tx_fee(
    tx_pool: &TxPool,
    snapshot: &Snapshot,
    rtx: &ResolvedTransaction,
    tx_size: usize,
) -> Result<Capacity, Reject> {
    let fee = DaoCalculator::new(snapshot.consensus(), &snapshot.borrow_as_data_loader())
        .transaction_fee(rtx)
        .map_err(|err| {
            Reject::Malformed(
                format!("{err}"),
                "expect (outputs capacity) <= (inputs capacity)".to_owned(),
            )
        })?;
    // Theoretically we cannot use size as weight directly to calculate fee_rate,
    // here min fee rate is used as a cheap check,
    // so we will use size to calculate fee_rate directly
    let min_fee = tx_pool.config.min_fee_rate.fee(tx_size as u64);
    // reject txs which fee lower than min fee rate
    if fee < min_fee {
        let reject =
            Reject::LowFeeRate(tx_pool.config.min_fee_rate, min_fee.as_u64(), fee.as_u64());
        ckb_logger::debug!("Reject tx {}", reject);
        return Err(reject);
    }
    Ok(fee)
}
```

**File:** tx-pool/src/component/entry.rs (L114-118)
```rust
    /// Returns fee rate
    pub fn fee_rate(&self) -> FeeRate {
        let weight = get_transaction_weight(self.size, self.cycles);
        FeeRate::calculate(self.fee, weight)
    }
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

**File:** util/types/src/core/tx_pool.rs (L276-303)
```rust
/// Equal to MAX_BLOCK_BYTES / MAX_BLOCK_CYCLES, see ckb-chain-spec.
/// The precision is set so that the difference between MAX_BLOCK_CYCLES * DEFAULT_BYTES_PER_CYCLES
/// and MAX_BLOCK_BYTES is less than 1.
pub const DEFAULT_BYTES_PER_CYCLES: f64 = 0.000_170_571_4_f64;

/// vbytes has been deprecated, renamed to weight to prevent ambiguity
#[deprecated(
    since = "0.107.0",
    note = "Please use the get_transaction_weight instead"
)]
pub fn get_transaction_virtual_bytes(tx_size: usize, cycles: u64) -> u64 {
    std::cmp::max(
        tx_size as u64,
        (cycles as f64 * DEFAULT_BYTES_PER_CYCLES) as u64,
    )
}

/// The miners select transactions to fill the limited block space which gives the highest fee.
/// Because there are two different limits, serialized size and consumed cycles,
/// the selection algorithm is a multi-dimensional knapsack problem.
/// Introducing the transaction weight converts the multi-dimensional knapsack to a typical knapsack problem,
/// which has a simple greedy algorithm.
pub fn get_transaction_weight(tx_size: usize, cycles: u64) -> u64 {
    std::cmp::max(
        tx_size as u64,
        (cycles as f64 * DEFAULT_BYTES_PER_CYCLES) as u64,
    )
}
```

**File:** tx-pool/src/process.rs (L286-294)
```rust
                let res = resolve_tx(tx_pool, &snapshot, tx.clone(), false);
                match res {
                    Ok((rtx, status)) => {
                        let fee = check_tx_fee(tx_pool, &snapshot, &rtx, tx_size)?;
                        Ok((tip_hash, rtx, status, fee, tx_size))
                    }
                    Err(Reject::Resolve(OutPointError::Dead(out))) => {
                        let (rtx, status) = resolve_tx(tx_pool, &snapshot, tx.clone(), true)?;
                        let fee = check_tx_fee(tx_pool, &snapshot, &rtx, tx_size)?;
```

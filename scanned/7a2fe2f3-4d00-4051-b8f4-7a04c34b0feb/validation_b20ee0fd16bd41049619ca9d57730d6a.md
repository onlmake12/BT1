### Title
Unit Mismatch in Minimum Fee Rate Check: `tx_size` Used as `weight` in `check_tx_fee` — (`tx-pool/src/util.rs`)

---

### Summary

In `check_tx_fee`, the minimum fee is computed by passing raw serialized byte size (`tx_size`) to `FeeRate::fee()`, which expects a **weight** argument (a composite of size and cycles). For cycle-heavy transactions, `weight > tx_size`, so the minimum fee threshold is systematically understated, allowing such transactions to enter the tx pool with fees below the intended minimum fee rate.

---

### Finding Description

`FeeRate` is defined as **shannons per kilo-weight**, where `weight = max(tx_size, cycles * DEFAULT_BYTES_PER_CYCLES)`. [1](#0-0) [2](#0-1) 

`FeeRate::fee(weight)` computes `fee = fee_rate * weight / 1000`. [3](#0-2) 

However, in `check_tx_fee`, the minimum fee is computed as:

```rust
let min_fee = tx_pool.config.min_fee_rate.fee(tx_size as u64);
``` [4](#0-3) 

`tx_size` is the raw serialized byte count, **not** the weight. The code comment itself acknowledges the mismatch: *"Theoretically we cannot use size as weight directly to calculate fee_rate, here min fee rate is used as a cheap check, so we will use size to calculate fee_rate directly."* [5](#0-4) 

This is the only fee rate admission check in the tx pool submission path (`pre_check` → `check_tx_fee`). There is no subsequent check using actual weight. [6](#0-5) 

For a cycle-heavy transaction where `cycles * DEFAULT_BYTES_PER_CYCLES > tx_size`, the actual weight exceeds `tx_size`. The minimum fee computed using `tx_size` is therefore lower than what the configured `min_fee_rate` actually requires. The ratio of understatement is `weight / tx_size`, which can be up to `MAX_BLOCK_CYCLES / MAX_BLOCK_BYTES ≈ 5.86×` (the inverse of `DEFAULT_BYTES_PER_CYCLES`). [2](#0-1) 

The `TxPoolInfo` documentation further reflects this inconsistency, describing `min_fee_rate` as *"Shannons per 1000 bytes transaction serialization size"* while the `FeeRate` type is defined as *"shannons per kilo-weight"* — two different units conflated at the admission boundary. [7](#0-6) 

---

### Impact Explanation

An attacker can craft cycle-heavy transactions (e.g., with computationally expensive lock/type scripts) that pass the `check_tx_fee` admission gate with fees significantly below the intended `min_fee_rate` threshold. In the extreme case (where cycles dominate), the effective minimum fee paid can be as low as `1/5.86` of the intended minimum. These transactions are admitted to the tx pool, consuming pool memory and relay bandwidth at a discount. This enables sustained tx pool spam at reduced cost, degrading node performance and potentially crowding out legitimate transactions.

---

### Likelihood Explanation

Any unprivileged RPC caller can submit transactions via `send_transaction`. Constructing a cycle-heavy transaction is straightforward: a script that performs many VM instructions (e.g., a loop) with a small serialized size achieves a high `cycles / tx_size` ratio. No special privileges, keys, or majority hashpower are required. The attack is repeatable and cheap.

---

### Recommendation

Replace the `tx_size`-based minimum fee check with the actual weight-based check, consistent with how `TxEntry::fee_rate()` and block assembly compute fee rates:

```rust
// In check_tx_fee, after computing `fee` and knowing `cycles` from the resolved tx:
let weight = get_transaction_weight(tx_size, cycles);
let min_fee = tx_pool.config.min_fee_rate.fee(weight);
```

If cycles are not yet available at the pre-check stage (before script execution), the check should be deferred to after `verify_rtx` returns the actual cycle count, or the `min_fee_rate` configuration and documentation should be explicitly scoped to size-only semantics and a separate weight-based check added post-verification.

---

### Proof of Concept

Consider `min_fee_rate = 1000` shannons/KW (the default) and a transaction with:
- `tx_size = 200` bytes
- `cycles = 5,000,000`

**Actual weight:**
```
weight = max(200, 5_000_000 * 0.000_170_571_4) = max(200, 852) = 852
```

**Minimum fee using actual weight (correct):**
```
min_fee = 1000 * 852 / 1000 = 852 shannons
```

**Minimum fee as computed by `check_tx_fee` (incorrect):**
```
min_fee = 1000 * 200 / 1000 = 200 shannons
```

A transaction paying only 200 shannons passes `check_tx_fee` and enters the tx pool, despite its true fee rate being `1000 * 200 / 852 ≈ 234` shannons/KW — well below the 1000 shannons/KW threshold. The attacker pays ~76% less than the intended minimum. [8](#0-7) [9](#0-8) [10](#0-9)

### Citations

**File:** util/types/src/core/fee_rate.rs (L3-7)
```rust
/// shannons per kilo-weight
#[derive(Clone, Copy, Default, Debug, PartialEq, Eq, PartialOrd, Ord)]
pub struct FeeRate(pub u64);

const KW: u64 = 1000;
```

**File:** util/types/src/core/fee_rate.rs (L11-16)
```rust
    pub fn calculate(fee: Capacity, weight: u64) -> Self {
        if weight == 0 {
            return FeeRate::zero();
        }
        FeeRate::from_u64(fee.as_u64().saturating_mul(KW) / weight)
    }
```

**File:** util/types/src/core/fee_rate.rs (L34-37)
```rust
    pub fn fee(self, weight: u64) -> Capacity {
        let fee = self.0.saturating_mul(weight) / KW;
        Capacity::shannons(fee)
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

**File:** util/types/src/core/tx_pool.rs (L339-348)
```rust
    /// Fee rate threshold. The pool rejects transactions which fee rate is below this threshold.
    ///
    /// The unit is Shannons per 1000 bytes transaction serialization size in the block.
    pub min_fee_rate: FeeRate,

    /// Min RBF rate threshold. The pool reject RBF transactions which fee rate is below this threshold.
    /// if min_rbf_rate > min_fee_rate then RBF is enabled on the node.
    ///
    /// The unit is Shannons per 1000 bytes transaction serialization size in the block.
    pub min_rbf_rate: FeeRate,
```

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

**File:** tx-pool/src/process.rs (L269-316)
```rust
    pub(crate) async fn pre_check(
        &self,
        tx: &TransactionView,
    ) -> (Result<PreCheckedTx, Reject>, Arc<Snapshot>) {
        // Acquire read lock for cheap check
        let tx_size = tx.data().serialized_size_in_block();

        let (ret, snapshot) = self
            .with_tx_pool_read_lock(|tx_pool, snapshot| {
                let tip_hash = snapshot.tip_hash();

                // Same txid means exactly the same transaction, including inputs, outputs, witnesses, etc.
                // It's also not possible for RBF, reject it directly
                check_txid_collision(tx_pool, tx)?;

                // Try normal path first, if double-spending check success we don't need RBF check
                // this make sure RBF won't introduce extra performance cost for hot path
                let res = resolve_tx(tx_pool, &snapshot, tx.clone(), false);
                match res {
                    Ok((rtx, status)) => {
                        let fee = check_tx_fee(tx_pool, &snapshot, &rtx, tx_size)?;
                        Ok((tip_hash, rtx, status, fee, tx_size))
                    }
                    Err(Reject::Resolve(OutPointError::Dead(out))) => {
                        let (rtx, status) = resolve_tx(tx_pool, &snapshot, tx.clone(), true)?;
                        let fee = check_tx_fee(tx_pool, &snapshot, &rtx, tx_size)?;
                        let conflicts = tx_pool.pool_map.find_conflict_outpoint(tx);
                        if conflicts.is_none() {
                            // this mean one input's outpoint is dead, but there is no direct conflicted tx in tx_pool
                            // we should reject it directly and don't need to put it into conflicts pool
                            error!(
                                "{} is resolved as Dead, but there is no conflicted tx",
                                rtx.transaction.proposal_short_id()
                            );
                            return Err(Reject::Resolve(OutPointError::Dead(out)));
                        }
                        // we also return Ok here, so that the entry will be continue to be verified before submit
                        // we only want to put it into conflicts pool after the verification stage passed
                        // then we will double-check conflicts txs in `submit_entry`

                        Ok((tip_hash, rtx, status, fee, tx_size))
                    }
                    Err(err) => Err(err),
                }
            })
            .await;
        (ret, snapshot)
    }
```

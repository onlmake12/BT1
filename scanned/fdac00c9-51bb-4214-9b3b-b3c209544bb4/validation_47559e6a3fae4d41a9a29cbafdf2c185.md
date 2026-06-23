### Title
Fee Rate Enforcement Uses Serialized Size Instead of Actual Weight, Allowing Cycle-Heavy Transactions to Bypass `min_fee_rate` - (File: `tx-pool/src/util.rs`)

---

### Summary

The `check_tx_fee` function in `tx-pool/src/util.rs` enforces the minimum fee rate using only the transaction's serialized byte size (`tx_size`), not the actual transaction weight (`max(tx_size, cycles * DEFAULT_BYTES_PER_CYCLES)`). Because cycles are unknown at pre-check time, the fee gate is bypassed for cycle-heavy transactions: a sender can craft a script-heavy transaction that passes the size-based fee check but, once verified, carries an effective fee rate far below `min_fee_rate`.

---

### Finding Description

`check_tx_fee` is called in `pre_check` before script execution, so cycles are not yet available. It computes the minimum fee as:

```rust
// tx-pool/src/util.rs:45
let min_fee = tx_pool.config.min_fee_rate.fee(tx_size as u64);
```

The code itself acknowledges the gap:

```rust
// Theoretically we cannot use size as weight directly to calculate fee_rate,
// here min fee rate is used as a cheap check,
// so we will use size to calculate fee_rate directly
``` [1](#0-0) 

After script verification completes, the actual cycles are known and a `TxEntry` is created with both `size` and `cycles`. The entry's real fee rate is computed via `get_transaction_weight`:

```rust
// tx-pool/src/component/entry.rs:115-118
pub fn fee_rate(&self) -> FeeRate {
    let weight = get_transaction_weight(self.size, self.cycles);
    FeeRate::calculate(self.fee, weight)
}
``` [2](#0-1) 

Where `get_transaction_weight` is:

```rust
pub fn get_transaction_weight(tx_size: usize, cycles: u64) -> u64 {
    std::cmp::max(
        tx_size as u64,
        (cycles as f64 * DEFAULT_BYTES_PER_CYCLES) as u64,
    )
}
``` [3](#0-2) 

There is **no second fee-rate check** after verification. The `TxEntry` is submitted directly with the actual cycles, and no code re-validates that `fee / weight >= min_fee_rate`. [4](#0-3) 

---

### Impact Explanation

For a cycle-heavy transaction where `cycles * DEFAULT_BYTES_PER_CYCLES > tx_size`, the actual weight exceeds the serialized size. The admission check passes because it only tests `fee >= min_fee_rate * tx_size`, but the effective fee rate stored in the pool entry is `fee / weight`, which can be arbitrarily below `min_fee_rate`.

**Concrete example** (with default `min_fee_rate = 1000 shannons/KW`):
- `tx_size = 1000 bytes`
- `cycles = 10,000,000`
- `weight = max(1000, 10,000,000 × 0.000_170_571_4) = 1705`
- Admission check: `fee >= 1000 * 1000 / 1000 = 1000 shannons` → passes with fee = 1000
- Actual fee rate: `1000 / 1705 ≈ 586 shannons/KW` — **44% below `min_fee_rate`**

Such transactions:
1. Pollute the tx-pool with below-minimum-fee-rate entries
2. Consume the block cycle budget at below-minimum effective fee rates
3. Displace legitimate higher-fee-rate transactions from block templates, degrading the fee market [5](#0-4) 

---

### Likelihood Explanation

Any unprivileged RPC caller (`send_transaction`) or P2P transaction relayer can trigger this. The attacker only needs to deploy a script that consumes many cycles (e.g., a tight loop) while keeping the serialized transaction small. The fee paid is proportional to `tx_size` (small), but the block resource consumed is proportional to `weight` (large). No special privileges, keys, or majority hashpower are required. [6](#0-5) 

---

### Recommendation

After script verification completes and actual cycles are known, re-validate the fee rate against the true weight before admitting the entry to the pool:

```rust
let weight = get_transaction_weight(tx_size, verified.cycles);
let min_fee_by_weight = tx_pool.config.min_fee_rate.fee(weight);
if fee < min_fee_by_weight {
    return Err(Reject::LowFeeRate(...));
}
```

This second check should occur in the `submit_entry` path, after `verify_rtx` returns the actual cycle count, mirroring the existing size-based check in `check_tx_fee`. [7](#0-6) 

---

### Proof of Concept

1. Deploy a lock script that executes a tight CKB-VM loop consuming ~10,000,000 cycles.
2. Create a transaction spending a cell locked by that script. Keep the transaction serialized size small (~1000 bytes).
3. Set the output capacity so that `fee = min_fee_rate * tx_size / 1000 = 1000 shannons` (just enough to pass the size-based check).
4. Submit via `send_transaction` RPC.
5. `check_tx_fee` passes: `1000 >= 1000 * 1000 / 1000`.
6. Script verification runs; actual cycles ≈ 10,000,000; weight ≈ 1705.
7. `TxEntry` is created and admitted. Its `fee_rate()` returns ≈ 586 shannons/KW — below `min_fee_rate = 1000`.
8. The transaction occupies the tx-pool and competes for block cycle budget at a sub-minimum effective fee rate. [8](#0-7) [2](#0-1)

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

**File:** tx-pool/src/component/entry.rs (L115-118)
```rust
    pub fn fee_rate(&self) -> FeeRate {
        let weight = get_transaction_weight(self.size, self.cycles);
        FeeRate::calculate(self.fee, weight)
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

**File:** tx-pool/src/process.rs (L371-379)
```rust
    pub(crate) async fn submit_remote_tx(
        &self,
        tx: TransactionView,
        declared_cycles: Cycle,
        peer: PeerIndex,
    ) -> Result<bool, Reject> {
        self.resumeble_process_tx_and_notify_full_reject(tx, false, Some((declared_cycles, peer)))
            .await
    }
```

### Title
`check_tx_fee` Enforces `min_fee_rate` Against Raw Serialized Size Instead of Actual Transaction Weight, Allowing Below-Minimum-Fee-Rate Transactions Into the Mempool — (File: `tx-pool/src/util.rs`)

---

### Summary

`check_tx_fee` in `tx-pool/src/util.rs` computes the minimum required fee using only the transaction's serialized byte size, but CKB's actual fee-rate metric is `fee / weight` where `weight = max(size, cycles × DEFAULT_BYTES_PER_CYCLES)`. For a high-cycle transaction, `weight >> size`, so the actual effective fee rate can be far below `min_fee_rate` even though the size-based check passes. No second weight-based fee-rate check is performed after cycles are known. An unprivileged tx-pool submitter can exploit this to admit below-minimum-fee-rate transactions into the mempool.

---

### Finding Description

`check_tx_fee` is the sole fee-rate gate for mempool admission:

```rust
// tx-pool/src/util.rs  lines 42-52
// Theoretically we cannot use size as weight directly to calculate fee_rate,
// here min fee rate is used as a cheap check,
// so we will use size to calculate fee_rate directly
let min_fee = tx_pool.config.min_fee_rate.fee(tx_size as u64);
// reject txs which fee lower than min fee rate
if fee < min_fee {
    ...
    return Err(reject);
}
``` [1](#0-0) 

The minimum fee is computed as `min_fee_rate × tx_size / 1000`. However, the canonical fee-rate metric used everywhere else in the codebase — block template selection, fee estimation, eviction — is:

```rust
// util/types/src/core/tx_pool.rs  lines 298-303
pub fn get_transaction_weight(tx_size: usize, cycles: u64) -> u64 {
    std::cmp::max(
        tx_size as u64,
        (cycles as f64 * DEFAULT_BYTES_PER_CYCLES) as u64,
    )
}
``` [2](#0-1) 

`TxEntry::fee_rate()` uses this weight-based calculation:

```rust
// tx-pool/src/component/entry.rs  lines 115-118
pub fn fee_rate(&self) -> FeeRate {
    let weight = get_transaction_weight(self.size, self.cycles);
    FeeRate::calculate(self.fee, weight)
}
``` [3](#0-2) 

The admission flow in `pre_check` calls `check_tx_fee` before cycles are known, then creates the `TxEntry` with actual cycles — but never re-checks the fee rate against the true weight:

```rust
// tx-pool/src/process.rs  lines 288-290
Ok((rtx, status)) => {
    let fee = check_tx_fee(tx_pool, &snapshot, &rtx, tx_size)?;
    Ok((tip_hash, rtx, status, fee, tx_size))
``` [4](#0-3) 

After `verify_rtx` returns the actual cycles, `TxEntry::new` is constructed and submitted with no second fee-rate gate.

---

### Impact Explanation

With `DEFAULT_BYTES_PER_CYCLES = 0.000_170_571_4` and `max_tx_verify_cycles = 70_000_000` (default), the maximum weight from cycles alone is ≈ 11,940 bytes. A transaction with a 200-byte serialized size and 70 M cycles has:

- **Size-based min fee** (what is checked): `1000 × 200 / 1000 = 200 shannons`
- **Actual weight**: `max(200, 11940) = 11940`
- **Actual fee rate**: `200 / 11940 × 1000 ≈ 16.7 shannons/KW`

The transaction passes admission (fee = 200 ≥ min_fee = 200) but its true fee rate is ~60× below `min_fee_rate`. Such transactions are never mined (miners sort by weight-based fee rate) but occupy mempool slots until expiry. An attacker can repeatedly submit such transactions to exhaust the mempool, causing legitimate transactions to be evicted via the pool-full eviction path.

**Impact: Medium** — mempool DoS; legitimate transactions are delayed or evicted; no direct fund loss.

---

### Likelihood Explanation

Any unprivileged user who can call `send_transaction` via RPC or relay a transaction over P2P can exploit this. Writing a script that consumes many cycles (e.g., a loop-heavy lock script) is straightforward. The attacker pays a small fee (just above `min_fee_rate × size`) and can submit many such transactions. The default `max_tx_pool_size` of 180 MB and `expiry_hours = 12` bound the window, but the attack is repeatable.

**Likelihood: Medium** — low cost, no special privilege required, repeatable.

---

### Recommendation

After `verify_rtx` returns the actual cycle count, perform a second fee-rate check using the true weight before admitting the entry to the pool:

```rust
// After TxEntry is constructed with actual cycles:
let actual_fee_rate = entry.fee_rate(); // uses get_transaction_weight(size, cycles)
if actual_fee_rate < tx_pool.config.min_fee_rate {
    return Err(Reject::LowFeeRate(...));
}
```

The existing size-based check in `check_tx_fee` can remain as a cheap early-exit for obviously under-fee transactions, but it must not be the sole gate.

---

### Proof of Concept

1. Write a CKB lock script that loops to consume ~70 M cycles (within `max_tx_verify_cycles`).
2. Build a transaction spending a cell locked by this script. Serialized size ≈ 200 bytes.
3. Set output capacity so that `fee = min_fee_rate × tx_size / 1000 = 200 shannons` (exactly the size-based minimum).
4. Submit via `send_transaction` RPC.
5. Observe: the transaction is accepted into the mempool (`check_tx_fee` passes because `fee(200) >= min_fee(200)`).
6. Observe: the transaction's actual fee rate = `200 / 11940 × 1000 ≈ 16.7 shannons/KW`, far below `min_fee_rate = 1000`.
7. Observe: the transaction is never selected for block templates (miners rank by weight-based fee rate).
8. Repeat to fill the mempool and evict legitimate transactions. [5](#0-4) [6](#0-5) [7](#0-6)

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

**File:** tx-pool/src/component/entry.rs (L115-118)
```rust
    pub fn fee_rate(&self) -> FeeRate {
        let weight = get_transaction_weight(self.size, self.cycles);
        FeeRate::calculate(self.fee, weight)
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

### Title
`check_tx_fee` Enforces `min_fee_rate` Using Only `tx_size` While Actual Fee Rate Uses `weight = max(size, cycles × bytes_per_cycle)`, Allowing High-Cycle Transactions to Bypass the Minimum Fee Rate Gate - (`File: tx-pool/src/util.rs`)

---

### Summary

The tx-pool admission gate in `check_tx_fee` computes the minimum required fee using only the serialized transaction size, while the actual fee rate stored in `TxEntry` and used for prioritization and eviction uses `get_transaction_weight(size, cycles)` — which is `max(tx_size, cycles × DEFAULT_BYTES_PER_CYCLES)`. An unprivileged RPC caller can craft a transaction whose script consumes many cycles but has a small serialized size, paying a fee that satisfies the size-only gate while the transaction's true fee rate is far below `min_fee_rate`. This is a direct analog to the external report's pattern: a user-controlled parameter (cycles) causes the fee check to use a different denominator than the actual fee rate, enabling fee evasion.

---

### Finding Description

In `tx-pool/src/util.rs`, `check_tx_fee` enforces the minimum fee rate as follows:

```rust
// Theoretically we cannot use size as weight directly to calculate fee_rate,
// here min fee rate is used as a cheap check,
// so we will use size to calculate fee_rate directly
let min_fee = tx_pool.config.min_fee_rate.fee(tx_size as u64);
if fee < min_fee {
    return Err(Reject::LowFeeRate(...));
}
``` [1](#0-0) 

The minimum fee is computed as `min_fee_rate × tx_size / 1000` (shannons), using only the byte size of the transaction.

However, once the transaction passes this gate and is admitted, its `TxEntry` stores the actual fee rate computed via `get_transaction_weight`:

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

with `DEFAULT_BYTES_PER_CYCLES = 0.000_170_571_4`. [4](#0-3) 

The same weight formula is used for scoring/eviction: [5](#0-4) 

The `check_tx_fee` gate is called in `pre_check` before script execution, so cycles are not yet known at that point — the gate uses only size: [6](#0-5) 

After verification, the entry is created with the actual verified cycles: [7](#0-6) 

**Concrete numeric example** with default config (`min_fee_rate = 1000` shannons/KW, `max_tx_verify_cycles = 70,000,000`):

| Parameter | Value |
|---|---|
| `tx_size` | 200 bytes (small tx) |
| `cycles` | 69,000,000 (near max) |
| `weight` | `max(200, 69_000_000 × 0.000_170_571_4)` ≈ **11,769 bytes** |
| `min_fee` (gate check) | `1000 × 200 / 1000` = **200 shannons** |
| Actual fee rate if fee = 200 | `200 × 1000 / 11,769` ≈ **16 shannons/KW** |
| Intended minimum | **1000 shannons/KW** |

The transaction passes the gate paying ~60× less than the intended minimum fee rate.

---

### Impact Explanation

A transaction that passes `check_tx_fee` with fee = `min_fee_rate × tx_size / 1000` but has high cycles enters the pool with an actual fee rate of `min_fee_rate × tx_size / weight`, which can be orders of magnitude below `min_fee_rate`. This means:

1. **Fee evasion**: The `min_fee_rate` policy is bypassed. Transactions enter the pool paying far less than the node operator intends to require.
2. **Pool resource consumption**: An attacker can fill the mempool with high-cycle, low-fee-rate transactions at minimal cost. These transactions consume pool space (counted by `total_tx_size`) and verification CPU.
3. **Miner revenue impact**: These transactions are deprioritized for block inclusion due to their low actual fee rate, but they displace legitimate higher-fee-rate transactions from the pool when `limit_size` eviction runs.

The `limit_size` eviction uses the actual fee rate (via `EvictKey`), so these transactions are evicted first when the pool is full — but they can still transiently occupy pool space and force eviction of other transactions. [8](#0-7) 

---

### Likelihood Explanation

- **Entry path**: Any unprivileged user can call `send_transaction` via the JSON-RPC API. No special role or key is required.
- **Ease of exploit**: Crafting a high-cycle script is straightforward — a RISC-V loop in a CKB lock or type script can consume an arbitrary number of cycles up to `max_tx_verify_cycles`. The attacker controls cycles directly through script content.
- **Deterministic**: The discrepancy is structural and always present when `cycles × DEFAULT_BYTES_PER_CYCLES > tx_size`, which is true for any transaction whose script consumes more than `tx_size / DEFAULT_BYTES_PER_CYCLES ≈ tx_size / 0.000_170_571_4` cycles.
- **No special conditions**: The default `min_fee_rate = 1000` and `max_tx_verify_cycles = 70,000,000` make this trivially exploitable on any standard node. [9](#0-8) 

---

### Recommendation

Replace the size-only minimum fee check in `check_tx_fee` with a weight-based check. Since cycles are not yet known at `pre_check` time (before script execution), one approach is to use the declared cycles (if provided by the relayer) or `max_tx_verify_cycles` as a conservative upper bound for the weight-based gate:

```rust
// Use declared_cycles or max_tx_verify_cycles as upper bound
let conservative_weight = get_transaction_weight(tx_size, max_cycles_bound);
let min_fee = tx_pool.config.min_fee_rate.fee(conservative_weight);
```

Alternatively, perform a second fee rate check after script execution (when actual cycles are known) and reject the transaction if its actual fee rate is below `min_fee_rate`. This is the correct enforcement point since `verified.cycles` is available at line 751 of `tx-pool/src/process.rs` before `submit_entry` is called. [10](#0-9) 

---

### Proof of Concept

1. Deploy a CKB lock script that executes a tight RISC-V loop consuming ~69,000,000 cycles (just under `max_tx_verify_cycles = 70,000,000`).
2. Create a transaction spending a cell locked by this script. The serialized transaction size will be small (e.g., ~200 bytes for a minimal transaction).
3. Set the transaction fee to `min_fee_rate × tx_size / 1000 = 1000 × 200 / 1000 = 200` shannons — just enough to pass `check_tx_fee`.
4. Submit via `send_transaction` RPC.
5. The transaction passes `check_tx_fee` (200 ≥ 200 shannons).
6. After script execution, the entry is created with `cycles ≈ 69,000,000`, giving `weight ≈ 11,769` bytes and actual `fee_rate ≈ 16` shannons/KW — 62× below `min_fee_rate`.
7. The transaction is admitted to the pool with a sub-minimum fee rate.
8. Repeat to fill the pool with low-fee-rate transactions at minimal cost. [11](#0-10) [12](#0-11)

### Citations

**File:** tx-pool/src/util.rs (L28-53)
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

**File:** tx-pool/src/process.rs (L269-315)
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
```

**File:** tx-pool/src/process.rs (L734-753)
```rust
        let verified = try_or_return_with_snapshot!(verified_ret, snapshot);

        if let Some(declared) = declared_cycles
            && declared != verified.cycles
        {
            info!(
                "process_tx declared cycles not match verified cycles, declared: {}, verified: {}, tx_hash: {}",
                declared,
                verified.cycles,
                tx.hash()
            );
            return Some((
                Err(Reject::DeclaredWrongCycles(declared, verified.cycles)),
                snapshot,
            ));
        }

        let entry = TxEntry::new(rtx, verified.cycles, fee, tx_size);

        let (ret, submit_snapshot) = self.submit_entry(tip_hash, entry, status).await;
```

**File:** tx-pool/src/pool.rs (L290-328)
```rust
    // Remove transactions from the pool until total size <= size_limit.
    // Return a `Reject` for current inserting entry if it's removed
    pub(crate) fn limit_size(
        &mut self,
        callbacks: &Callbacks,
        current_entry_id: Option<&ProposalShortId>,
    ) -> Option<Reject> {
        let mut ret = None;
        while self.pool_map.total_tx_size > self.config.max_tx_pool_size {
            let next_evict_entry = || {
                self.pool_map
                    .next_evict_entry(Status::Pending)
                    .or_else(|| self.pool_map.next_evict_entry(Status::Gap))
                    .or_else(|| self.pool_map.next_evict_entry(Status::Proposed))
            };

            if let Some(id) = next_evict_entry() {
                let removed = self.pool_map.remove_entry_and_descendants(&id);
                for entry in removed {
                    let tx_hash = entry.transaction().hash();
                    debug!(
                        "Removed by size limit {} timestamp({})",
                        tx_hash, entry.timestamp
                    );
                    let reject = Reject::Full(format!(
                        "the fee_rate for this transaction is: {}",
                        entry.fee_rate()
                    ));
                    if let Some(short_id) = current_entry_id
                        && entry.proposal_short_id() == *short_id
                    {
                        ret = Some(reject.clone());
                    }
                    callbacks.call_reject(self, &entry, reject);
                }
            }
        }
        self.pool_map.entries.shrink_to_fit();
        ret
```

**File:** util/app-config/src/legacy/tx_pool.rs (L10-14)
```rust
const DEFAULT_MIN_FEE_RATE: FeeRate = FeeRate::from_u64(1000);
// default min rbf rate, 1500 shannons per kilobyte
const DEFAULT_MIN_RBF_RATE: FeeRate = FeeRate::from_u64(1500);
// default max tx verify cycles
const DEFAULT_MAX_TX_VERIFY_CYCLES: Cycle = TWO_IN_TWO_OUT_CYCLES * 20;
```

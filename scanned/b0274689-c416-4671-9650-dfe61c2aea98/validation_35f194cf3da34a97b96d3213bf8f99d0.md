### Title
Single-Dimension Fee Rate Admission Check Ignores Cycle Weight, Allowing Below-Minimum Fee Rate Transactions Into the Pool - (File: `tx-pool/src/util.rs`)

### Summary

The tx-pool admission check in `check_tx_fee` enforces `min_fee_rate` using only the serialized transaction **size** as the weight. However, the canonical fee rate used everywhere else in the pool (eviction, block assembly, fee estimation) uses `get_transaction_weight(size, cycles)` = `max(size, cycles × DEFAULT_BYTES_PER_CYCLES)`. For cycle-heavy transactions the two weights diverge by orders of magnitude, allowing an unprivileged submitter to pass the admission gate with a fee rate far below the configured `min_fee_rate`.

### Finding Description

`check_tx_fee` in `tx-pool/src/util.rs` computes the minimum required fee as:

```rust
let min_fee = tx_pool.config.min_fee_rate.fee(tx_size as u64);
``` [1](#0-0) 

The code comment even acknowledges the gap: *"Theoretically we cannot use size as weight directly to calculate fee_rate, here min fee rate is used as a cheap check."* [2](#0-1) 

Everywhere else in the pool the canonical weight is computed as:

```rust
pub fn get_transaction_weight(tx_size: usize, cycles: u64) -> u64 {
    std::cmp::max(
        tx_size as u64,
        (cycles as f64 * DEFAULT_BYTES_PER_CYCLES) as u64,
    )
}
``` [3](#0-2) 

with `DEFAULT_BYTES_PER_CYCLES = 0.000_170_571_4`. [4](#0-3) 

`TxEntry::fee_rate()` uses this two-dimensional weight for eviction and block-assembly scoring:

```rust
pub fn fee_rate(&self) -> FeeRate {
    let weight = get_transaction_weight(self.size, self.cycles);
    FeeRate::calculate(self.fee, weight)
}
``` [5](#0-4) 

The pool-size eviction loop also uses only `total_tx_size` (bytes), not cycles, as the eviction trigger:

```rust
while self.pool_map.total_tx_size > self.config.max_tx_pool_size {
``` [6](#0-5) 

There is no post-verification fee-rate re-check using the actual cycle-adjusted weight. `check_tx_fee` is the sole admission gate. [7](#0-6) 

### Impact Explanation

**Concrete numbers** (default config: `min_fee_rate = 1000` shannons/KB, `max_tx_verify_cycles = 70_000_000`):

| Parameter | Value |
|---|---|
| tx serialized size | 200 bytes |
| declared/actual cycles | 70 000 000 |
| size-based weight | 200 |
| cycle-adjusted weight | max(200, 70 000 000 × 0.000 170 571) = **11 940** |
| fee required by `check_tx_fee` | 1000 × 200 / 1000 = **200 shannons** |
| actual fee rate after admission | 200 × 1000 / 11 940 ≈ **16.75 shannons/KB** |

The admitted transaction carries a fee rate ~60× below the configured floor. Because `total_tx_size` is the only eviction trigger and the transaction is small (200 bytes), it occupies negligible byte-space in the pool while consuming the maximum allowed verification cycles. An attacker can flood the verify queue and pool with many such transactions, each paying only the size-floor fee, saturating the verification workers (`max_tx_verify_workers`) and degrading pool throughput for legitimate transactions. [8](#0-7) 

### Likelihood Explanation

The attack requires only an RPC call to `send_transaction` — no privileged access, no key material, no majority hashpower. The attacker must craft a transaction whose script genuinely consumes close to `max_tx_verify_cycles` cycles (e.g., a tight loop in a CKB-VM script). This is straightforward for any script author. The discrepancy grows with cycles, so the attacker naturally targets the maximum cycle budget. The default `max_tx_verify_cycles = 70_000_000` is large enough to produce the ~60× weight gap shown above. [9](#0-8) 

### Recommendation

Replace the size-only weight in `check_tx_fee` with the same two-dimensional weight used everywhere else. Because cycles are declared by the submitter at submission time (before script execution), the declared cycle count is available and can be used for the admission check:

```rust
// use declared cycles for the pre-verification weight estimate
let weight = get_transaction_weight(tx_size, declared_cycles);
let min_fee = tx_pool.config.min_fee_rate.fee(weight);
```

After script execution, re-validate the fee rate against the **actual** cycles to close the remaining gap between declared and actual cycles.

### Proof of Concept

1. Author a CKB lock/type script that runs a tight loop consuming exactly `max_tx_verify_cycles - ε` cycles (e.g., 69 999 999 cycles). Deploy it on testnet.
2. Construct a transaction spending a cell locked by that script. Serialized size ≈ 200 bytes.
3. Set the transaction fee to `ceil(min_fee_rate × tx_size / 1000)` = 200 shannons (at default 1000 shannons/KB).
4. Submit via `send_transaction`. `check_tx_fee` computes `min_fee = 1000 × 200 / 1000 = 200` shannons; the transaction passes.
5. After verification the pool records `fee_rate = 200 × 1000 / 11 940 ≈ 16 shannons/KB` — 60× below `min_fee_rate`.
6. Repeat with many UTXOs. Each transaction occupies only 200 bytes of `total_tx_size` (never triggering the byte-based eviction) while consuming 70 M cycles of verification work per entry, saturating the verification worker pool and delaying or starving legitimate high-fee transactions. [7](#0-6) [10](#0-9) [11](#0-10)

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

**File:** tx-pool/src/component/verify_queue.rs (L56-65)
```rust
pub(crate) struct VerifyQueue {
    /// inner tx entry
    inner: MultiIndexVerifyEntryMap,
    /// subscribe this notify to get be notified when there is item in the queue
    ready_rx: Arc<Notify>,
    /// total tx size in the queue, will reject new transaction if exceed the limit
    total_tx_size: usize,
    /// large cycle threshold, from `pool_config.max_tx_verify_cycles`
    large_cycle_threshold: u64,
}
```

**File:** util/app-config/src/legacy/tx_pool.rs (L14-14)
```rust
const DEFAULT_MAX_TX_VERIFY_CYCLES: Cycle = TWO_IN_TWO_OUT_CYCLES * 20;
```

### Title
Tx-Pool Admission Uses Size-Only Weight While Scoring/Eviction Uses Cycle-Adjusted Weight, Allowing Sub-Minimum-Fee-Rate Transactions to Enter the Pool — (File: `tx-pool/src/util.rs`)

### Summary

The tx-pool enforces `min_fee_rate` at admission using raw serialized byte size as the weight denominator, but all post-admission scoring, prioritization, and eviction logic uses `get_transaction_weight(size, cycles)` = `max(size, cycles × DEFAULT_BYTES_PER_CYCLES)`. For a cycle-heavy, byte-light transaction, these two weight values diverge by up to ~24×, allowing an unprivileged submitter to craft transactions that pass the admission gate while carrying an effective fee rate far below `min_fee_rate`.

### Finding Description

**Admission path** — `check_tx_fee` in `tx-pool/src/util.rs`:

```rust
// Theoretically we cannot use size as weight directly to calculate fee_rate,
// here min fee rate is used as a cheap check,
// so we will use size to calculate fee_rate directly
let min_fee = tx_pool.config.min_fee_rate.fee(tx_size as u64);
if fee < min_fee { ... }
```

The minimum required fee is `min_fee_rate × tx_size / 1000` shannons.

**Post-admission scoring/eviction** — `TxEntry::fee_rate()` in `tx-pool/src/component/entry.rs` and `EvictKey` construction:

```rust
pub fn fee_rate(&self) -> FeeRate {
    let weight = get_transaction_weight(self.size, self.cycles);
    FeeRate::calculate(self.fee, weight)
}
```

`get_transaction_weight` is defined in `util/types/src/core/tx_pool.rs`:

```rust
pub const DEFAULT_BYTES_PER_CYCLES: f64 = 0.000_170_571_4_f64;

pub fn get_transaction_weight(tx_size: usize, cycles: u64) -> u64 {
    std::cmp::max(
        tx_size as u64,
        (cycles as f64 * DEFAULT_BYTES_PER_CYCLES) as u64,
    )
}
```

For a transaction with `size = 500 bytes` and `cycles = 70,000,000` (the configured `max_tx_verify_cycles`):

| Step | Weight used | Min fee required |
|---|---|---|
| Admission (`check_tx_fee`) | 500 (size only) | `min_fee_rate × 500 / 1000` |
| Scoring/eviction (`fee_rate()`) | `max(500, 70_000_000 × 0.000_170_571_4)` = **11,940** | `min_fee_rate × 11,940 / 1000` |

The effective fee rate after admission is `500 / 11,940 ≈ 4.2%` of `min_fee_rate`. The code comment itself acknowledges this is a known approximation: *"Theoretically we cannot use size as weight directly to calculate fee_rate."*

### Impact Explanation

An unprivileged tx-pool submitter (via RPC `send_transaction` or P2P relay) can submit cycle-heavy, byte-light transactions that:

1. **Pass the admission fee check** — because `check_tx_fee` uses size-only weight, the required fee is ~24× lower than what the `min_fee_rate` policy intends for a cycle-heavy transaction.
2. **Occupy pool resources at below-minimum effective fee rate** — the pool's `total_tx_size` counter tracks byte size, so these transactions appear small, but they consume disproportionate cycle budget in the block.
3. **Distort fee estimation** — `get_fee_rate_statistics` (RPC) uses `get_transaction_weight` for confirmed blocks, while admitted transactions were checked against size-only weight. This creates a systematic downward bias in fee estimates when cycle-heavy transactions are prevalent.
4. **Eviction asymmetry** — `limit_size` evicts by `EvictKey` (weight-based fee rate), so these transactions are evicted first when the pool fills, but they can transiently displace legitimate transactions and cause spurious `PoolIsFull` rejections for honest submitters.

### Likelihood Explanation

The attack is straightforward: craft a transaction with a script that consumes close to `max_tx_verify_cycles` (70M cycles) but has a small serialized size. Pay only `min_fee_rate × size / 1000` shannons. The transaction passes admission. This requires no special privilege — any RPC caller or P2P peer can submit transactions. The divergence is largest at high cycle counts, which are reachable by any script author.

### Recommendation

Replace the size-only weight in `check_tx_fee` with `get_transaction_weight(tx_size, declared_cycles)` so that the admission gate enforces the same fee-rate semantics as scoring and eviction. If cycles are not yet known at pre-check time (before script execution), use the caller-declared cycle limit as a conservative upper bound, consistent with how `max_tx_verify_cycles` is already enforced.

### Proof of Concept

1. Construct a CKB transaction with a lock script that loops for ~70,000,000 cycles. Serialized size: ~500 bytes.
2. Set the transaction fee to `min_fee_rate × 500 / 1000` shannons (e.g., with default `min_fee_rate = 1000`, fee = 0.5 shannons — round up to 1 shannon).
3. Submit via `send_transaction` RPC.
4. **Expected (current behavior):** Transaction is admitted — `check_tx_fee` computes `min_fee = 1000 × 500 / 1000 = 500 shannons`; if fee ≥ 500, it passes.
5. **Observed inconsistency:** `TxEntry::fee_rate()` computes weight = `max(500, 70_000_000 × 0.000_170_571_4)` = 11,940; effective fee rate = `500 × 1000 / 11,940` ≈ **41 shannons/KW**, which is ~4.1% of the 1,000 shannons/KW `min_fee_rate`.
6. Repeat with many such transactions to fill the pool with sub-minimum-effective-fee-rate entries, degrading pool quality for honest users.

**Key files and lines:**

- Admission check (size-only weight): [1](#0-0) 
- Post-admission fee rate (cycle-adjusted weight): [2](#0-1) 
- `get_transaction_weight` definition: [3](#0-2) 
- `DEFAULT_BYTES_PER_CYCLES` constant: [4](#0-3) 
- Eviction uses weight-based fee rate: [5](#0-4) 
- Pool size eviction loop: [6](#0-5)

### Citations

**File:** tx-pool/src/util.rs (L42-52)
```rust
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
```

**File:** tx-pool/src/component/entry.rs (L114-118)
```rust
    /// Returns fee rate
    pub fn fee_rate(&self) -> FeeRate {
        let weight = get_transaction_weight(self.size, self.cycles);
        FeeRate::calculate(self.fee, weight)
    }
```

**File:** tx-pool/src/component/entry.rs (L234-247)
```rust
impl From<&TxEntry> for EvictKey {
    fn from(entry: &TxEntry) -> Self {
        let weight = get_transaction_weight(entry.size, entry.cycles);
        let descendants_weight =
            get_transaction_weight(entry.descendants_size, entry.descendants_cycles);

        let descendants_feerate = FeeRate::calculate(entry.descendants_fee, descendants_weight);
        let feerate = FeeRate::calculate(entry.fee, weight);
        EvictKey {
            fee_rate: descendants_feerate.max(feerate),
            timestamp: entry.timestamp,
            descendants_count: entry.descendants_count,
        }
    }
```

**File:** util/types/src/core/tx_pool.rs (L279-279)
```rust
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

**File:** tx-pool/src/pool.rs (L292-328)
```rust
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

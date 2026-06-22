Based on my investigation of the CKB codebase, I found a valid analog in the tx-pool subsystem.

---

### Title
No Safety Buffer Between Tx-Pool Admission Threshold and Eviction Threshold — (`tx-pool/src/util.rs`, `tx-pool/src/pool.rs`)

### Summary
The tx-pool uses the same `min_fee_rate` value as both the admission floor (in `check_tx_fee`) and the implicit eviction floor (in `limit_size`). A transaction submitted at exactly `min_fee_rate` when the pool is at capacity is admitted, then immediately evicted along with all its descendants. There is no safety buffer between the two thresholds, mirroring the original report's pattern of a single rate used for both "allow" and "trigger removal."

### Finding Description

**Admission check** — `tx-pool/src/util.rs`, `check_tx_fee`:

```rust
let min_fee = tx_pool.config.min_fee_rate.fee(tx_size as u64);
if fee < min_fee {
    return Err(Reject::LowFeeRate(...));
}
```

A transaction with `fee == min_fee` (i.e., fee rate exactly equal to `min_fee_rate`) passes this gate and is inserted into the pool. [1](#0-0) 

**Eviction logic** — `tx-pool/src/pool.rs`, `limit_size`:

```rust
while self.pool_map.total_tx_size > self.config.max_tx_pool_size {
    if let Some(id) = next_evict_entry() {
        let removed = self.pool_map.remove_entry_and_descendants(&id);
        ...
    }
}
```

`next_evict_entry` selects the entry with the **lowest `EvictKey`**, which sorts primarily by `fee_rate` ascending. The transaction admitted at exactly `min_fee_rate` is therefore the first candidate for eviction. Critically, `remove_entry_and_descendants` removes the evicted entry **and all its descendants**, regardless of those descendants' individual fee rates. [2](#0-1) 

The `EvictKey` ordering confirms lowest fee_rate is evicted first: [3](#0-2) 

There is no separate, higher "eviction threshold" — the same `min_fee_rate` that gates admission is the floor at which eviction begins. Unlike Bitcoin Core's mempool, which maintains a rolling minimum fee rate that rises above the static floor when the pool is full, CKB has no such buffer.

### Impact Explanation

1. A transaction submitted at exactly `min_fee_rate` (the documented minimum) is admitted, then immediately evicted when the pool is at capacity. The submitter receives `Reject::Full` despite paying the advertised minimum.
2. All descendants of the evicted transaction are also removed via `remove_entry_and_descendants`, even if those children carry higher fee rates. A user who built a transaction chain where the root pays `min_fee_rate` and children pay higher rates loses the entire chain.
3. The user can resubmit indefinitely at `min_fee_rate` and be evicted each time — a repeated-failure loop analogous to the original report's repeated liquidations.

The impact is denial-of-service at the tx-pool level for legitimate submitters who follow the documented `min_fee_rate` contract, and silent loss of descendant transactions that individually would have survived eviction.

### Likelihood Explanation

The pool reaching `max_tx_pool_size` (default 180 MB) is a realistic condition during periods of high network activity or spam. Any RPC caller (`send_transaction`) can trigger this path. No special privilege is required — an unprivileged transaction sender is the attacker-controlled entry point. [4](#0-3) 

### Recommendation

Introduce a separate, higher eviction floor — analogous to a "liquidation threshold" above the LTV ratio. Concretely:

- Define an `eviction_fee_rate` (e.g., `min_fee_rate * 1.1`) that is strictly greater than `min_fee_rate`.
- In `limit_size`, only evict entries whose `fee_rate < eviction_fee_rate`, not all entries at or above `min_fee_rate`.
- Alternatively, adopt Bitcoin Core's approach of a dynamic rolling minimum fee rate that rises when the pool is full, so newly admitted transactions are guaranteed a grace period before becoming eviction candidates.

This ensures a transaction admitted at `min_fee_rate` is not immediately eligible for eviction, and that descendant chains are not silently destroyed.

### Proof of Concept

1. Fill the pool to `max_tx_pool_size` with transactions at fee rate `min_fee_rate + 1`.
2. Submit `tx_A` at exactly `min_fee_rate` via `send_transaction` RPC.
3. `check_tx_fee` passes (`fee >= min_fee`); `tx_A` is inserted into the pool.
4. `limit_size` is called; `next_evict_entry` selects `tx_A` (lowest fee_rate).
5. `remove_entry_and_descendants` removes `tx_A` and any children.
6. Caller receives `Reject::Full("the fee_rate for this transaction is: ...")`.
7. Repeat from step 2 — the submitter is trapped in an eviction loop despite paying the documented minimum fee rate. [5](#0-4) [6](#0-5)

### Citations

**File:** tx-pool/src/util.rs (L44-52)
```rust
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

**File:** tx-pool/src/component/sort_key.rs (L92-103)
```rust
impl Ord for EvictKey {
    fn cmp(&self, other: &Self) -> Ordering {
        if self.fee_rate == other.fee_rate {
            if self.descendants_count == other.descendants_count {
                self.timestamp.cmp(&other.timestamp)
            } else {
                self.descendants_count.cmp(&other.descendants_count)
            }
        } else {
            self.fee_rate.cmp(&other.fee_rate)
        }
    }
```

**File:** util/app-config/src/legacy/tx_pool.rs (L9-20)
```rust
// default min fee rate, 1000 shannons per kilobyte
const DEFAULT_MIN_FEE_RATE: FeeRate = FeeRate::from_u64(1000);
// default min rbf rate, 1500 shannons per kilobyte
const DEFAULT_MIN_RBF_RATE: FeeRate = FeeRate::from_u64(1500);
// default max tx verify cycles
const DEFAULT_MAX_TX_VERIFY_CYCLES: Cycle = TWO_IN_TWO_OUT_CYCLES * 20;
// default max ancestors count
const DEFAULT_MAX_ANCESTORS_COUNT: usize = 1_000;
// Default expiration time for pool transactions in hours
const DEFAULT_EXPIRY_HOURS: u8 = 12;
// Default max_tx_pool_size 180mb
const DEFAULT_MAX_TX_POOL_SIZE: usize = 180_000_000;
```

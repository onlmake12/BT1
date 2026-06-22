### Title
Tx-Pool Size Limit Checked After Insertion, Allowing Transient Overshoot - (File: tx-pool/src/pool.rs, tx-pool/src/component/pool_map.rs)

### Summary
The `max_tx_pool_size` limit in CKB's transaction pool is enforced **after** a new transaction is already inserted into the pool, not before. This mirrors the IgnitionCore.sol pattern exactly: the cap check does not include the currently-being-added value, so the limit can be exceeded at least once per insertion.

### Finding Description

When a transaction is admitted to the pool, `pool_map.add_entry()` is called. Inside it, `updated_stat_for_add_tx` computes the new totals but only guards against arithmetic overflow — it does **not** compare against `max_tx_pool_size`: [1](#0-0) 

The transaction is then unconditionally inserted and `total_tx_size` is updated: [2](#0-1) 

Only **after** the entry is already in the pool does `limit_size` run its eviction loop: [3](#0-2) 

The guard condition `self.pool_map.total_tx_size > self.config.max_tx_pool_size` is evaluated when `total_tx_size` already includes the newly inserted transaction. The pool has already exceeded the configured limit at the moment the check runs.

Contrast this with the `VerifyQueue`, which correctly checks **before** insertion: [4](#0-3) [5](#0-4) 

The main pool (`PoolMap`) has no equivalent pre-insertion guard against `max_tx_pool_size`.

### Impact Explanation

An unprivileged RPC caller using `send_transaction` can cause the node's tx-pool to transiently hold more data than `max_tx_pool_size` allows. The maximum single-transaction size is `TRANSACTION_SIZE_LIMIT = 512 KB`: [6](#0-5) 

So the pool can reach `max_tx_pool_size + 512 KB` before eviction corrects it. The default `max_tx_pool_size` is 180 MB: [7](#0-6) 

The overshoot is bounded per insertion but is real: the configured memory ceiling is violated on every transaction that pushes the pool over the limit, causing unintended memory pressure on the node.

### Likelihood Explanation

Any unprivileged peer or RPC caller submitting transactions to a near-full pool triggers this on every admission. No special privileges, keys, or majority hashpower are required. The entry path is the standard `send_transaction` RPC or P2P relay path, both of which are fully reachable by an external actor.

### Recommendation

Add a pre-insertion check against `max_tx_pool_size` inside `add_entry` (or its caller) before calling `insert_entry`, analogous to the guard already present in `VerifyQueue::add_tx`. The check should be:

```rust
if self.total_tx_size.saturating_add(entry.size) > self.config.max_tx_pool_size {
    return Err(Reject::Full(...));
}
```

This ensures the limit is never exceeded, rather than corrected after the fact.

### Proof of Concept

1. Fill the pool to exactly `max_tx_pool_size - 1 byte` (achievable by submitting transactions up to that total).
2. Submit one more transaction of size `S > 1 byte` via `send_transaction`.
3. `add_entry` inserts it; `total_tx_size` becomes `max_tx_pool_size - 1 + S`, exceeding the limit.
4. `limit_size` is then called and evicts entries — but the pool has already transiently held `max_tx_pool_size + (S - 1)` bytes.
5. Repeat with maximum-size transactions (`S ≈ 512 KB`) to maximize the overshoot on each cycle.

### Citations

**File:** tx-pool/src/component/pool_map.rs (L200-221)
```rust
    pub(crate) fn add_entry(
        &mut self,
        mut entry: TxEntry,
        status: Status,
    ) -> Result<(bool, HashSet<TxEntry>), Reject> {
        let tx_short_id = entry.proposal_short_id();
        let mut evicts = Default::default();
        if self.entries.get_by_id(&tx_short_id).is_some() {
            return Ok((false, evicts));
        }
        let (total_tx_size, total_tx_cycles) =
            self.updated_stat_for_add_tx(entry.size, entry.cycles)?;
        trace!("pool_map.add_{:?} {}", status, entry.transaction().hash());
        evicts = self.check_and_record_ancestors(&mut entry)?;
        self.record_entry_edges(&entry)?;
        self.insert_entry(&entry, status);
        self.record_entry_descendants(&entry);
        self.track_entry_statics(None, Some(status));
        self.total_tx_size = total_tx_size;
        self.total_tx_cycles = total_tx_cycles;
        Ok((true, evicts))
    }
```

**File:** tx-pool/src/component/pool_map.rs (L710-729)
```rust
    /// Calculate size and cycles statistics for adding a tx.
    fn updated_stat_for_add_tx(
        &self,
        tx_size: usize,
        cycles: Cycle,
    ) -> Result<(usize, Cycle), Reject> {
        let total_tx_size = self.total_tx_size.checked_add(tx_size).ok_or_else(|| {
            Reject::Full(format!(
                "tx-pool total_tx_size {} overflows by add {}",
                self.total_tx_size, tx_size
            ))
        })?;
        let total_tx_cycles = self.total_tx_cycles.checked_add(cycles).ok_or_else(|| {
            Reject::Full(format!(
                "tx-pool total_tx_cycles {} overflows by add {}",
                self.total_tx_cycles, cycles
            ))
        })?;
        Ok((total_tx_size, total_tx_cycles))
    }
```

**File:** tx-pool/src/pool.rs (L292-329)
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
    }
```

**File:** tx-pool/src/component/verify_queue.rs (L103-106)
```rust
    /// Returns true if the queue is full.
    pub fn is_full(&self, add_tx_size: usize) -> bool {
        add_tx_size >= DEFAULT_MAX_VERIFY_QUEUE_TX_SIZE - self.total_tx_size
    }
```

**File:** tx-pool/src/component/verify_queue.rs (L215-220)
```rust
        if self.is_full(tx_size) {
            return Err(Reject::Full(format!(
                "verify_queue total_tx_size exceeded, failed to add tx: {:#x}",
                tx.hash()
            )));
        }
```

**File:** util/types/src/core/tx_pool.rs (L306-309)
```rust
/// The ckb consensus does not limit the size of a single transaction,
/// but if the size of the transaction is close to the limit of the block,
/// it may cause the transaction to fail to be packed
pub const TRANSACTION_SIZE_LIMIT: u64 = 512 * 1_000;
```

**File:** util/app-config/src/legacy/tx_pool.rs (L19-20)
```rust
// Default max_tx_pool_size 180mb
const DEFAULT_MAX_TX_POOL_SIZE: usize = 180_000_000;
```

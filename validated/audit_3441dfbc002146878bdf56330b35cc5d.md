### Title
TxEntry Timestamp Reset During Chain Reorganization Disrupts Expiry and Eviction Ordering - (File: tx-pool/src/process.rs)

### Summary

When a chain reorganization occurs, transactions from detached blocks are re-added to the tx-pool via `readd_detached_tx`. This function creates a brand-new `TxEntry` with a fresh `unix_time_as_millis()` timestamp, silently resetting the pool-entry age. The `timestamp` field governs both the expiry deadline and the eviction priority of every pool entry. Resetting it during what is logically a transparent intermediate operation (block inclusion → reorg → pool re-admission) is the direct CKB analog of the `lastTokenTransferredTimestamp` reset described in the reference report.

### Finding Description

`TxEntry::new()` always stamps the entry with the current wall-clock time: [1](#0-0) 

`readd_detached_tx` calls this constructor unconditionally for every transaction recovered from a detached block: [2](#0-1) 

By contrast, `remove_by_detached_proposal` — which handles the symmetric case of proposals being detached — reuses the existing `TxEntry` object and only calls `reset_statistic_state()`, which resets ancestor/descendant accounting but **preserves** `timestamp`: [3](#0-2) 

The `timestamp` field has two downstream consumers:

**1. Expiry eviction** (`remove_expired`): [4](#0-3) 

A transaction that entered the pool 11 hours ago (1 hour before the configured `expiry_hours` deadline) and was then included in a block that subsequently gets detached will be re-admitted with `timestamp = now`. Its expiry clock resets to a full `expiry_hours` window. Crucially, `remove_expired` is called inside `_update_tx_pool_for_reorg` **before** `readd_detached_tx`, so the freshly re-stamped entries bypass the expiry check entirely until the next block: [5](#0-4) 

**2. Eviction ordering** (`EvictKey`): [6](#0-5) 

`next_evict_entry` picks the entry with the **smallest** `EvictKey` (lowest fee-rate, then oldest timestamp): [7](#0-6) 

A re-admitted transaction whose timestamp was reset to `now` appears newer than other pool entries with the same fee-rate, so it is deprioritized for eviction. Entries that genuinely arrived later — and should survive longer — may be evicted first instead.

### Impact Explanation

- **Expiry bypass**: A low-fee transaction near its expiry deadline survives a reorg and re-enters the pool with a full fresh expiry window. Repeated natural reorgs can keep such transactions alive indefinitely, gradually filling the pool.
- **Eviction order inversion**: When `limit_size` must shed entries to make room for a new submission, the eviction cursor skips over re-admitted "zombie" entries (which appear newest) and may instead evict a legitimately newer, higher-priority entry. A submitter whose transaction is evicted in this way must resubmit and pay again.
- **Post-reorg pool overflow**: `limit_size` is called inside `_update_tx_pool_for_reorg` before `readd_detached_tx`, so the pool can transiently exceed `max_tx_pool_size` after re-admission with no immediate size enforcement. [8](#0-7) 

### Likelihood Explanation

Chain reorganizations of one or two blocks occur naturally on mainnet without any attacker involvement. Every such reorg triggers `update_tx_pool_for_reorg` → `readd_detached_tx`. A transaction submitter who keeps a low-fee transaction near its expiry window will have its deadline silently extended on every reorg that touches its containing block. No special privileges, hashpower, or key material are required; the entry path is the standard block-processing pipeline reachable by any peer that relays a valid competing chain tip.

### Recommendation

Preserve the original pool-entry timestamp when re-admitting detached transactions, mirroring the behavior of `remove_by_detached_proposal`. One approach: record the `(tx_hash → timestamp)` mapping before removing committed transactions, then pass it into `readd_detached_tx` and use `TxEntry::new_with_timestamp` with the recovered value when available, falling back to `unix_time_as_millis()` only for transactions that have no prior pool history. [9](#0-8) 

### Proof of Concept

1. Submit transaction `T` with fee-rate just above `min_fee_rate`; note its pool timestamp `t0`.
2. Mine a block that includes `T`; `T` is removed from the pool via `remove_committed_tx`.
3. A competing chain of equal or greater work arrives (natural reorg); the block containing `T` is detached.
4. `readd_detached_tx` re-admits `T` with `timestamp = now` (`t1 >> t0`).
5. Observe via `get_transaction` RPC that `time_added_to_pool` has jumped forward to `t1`.
6. Repeat steps 2–4 before `t1 + expiry_hours` elapses; `T

### Citations

**File:** tx-pool/src/component/entry.rs (L46-50)
```rust
impl TxEntry {
    /// Create new transaction pool entry
    pub fn new(rtx: Arc<ResolvedTransaction>, cycles: Cycle, fee: Capacity, size: usize) -> Self {
        Self::new_with_timestamp(rtx, cycles, fee, size, unix_time_as_millis())
    }
```

**File:** tx-pool/src/component/entry.rs (L52-75)
```rust
    /// Create new transaction pool entry with specified timestamp
    pub fn new_with_timestamp(
        rtx: Arc<ResolvedTransaction>,
        cycles: Cycle,
        fee: Capacity,
        size: usize,
        timestamp: u64,
    ) -> Self {
        TxEntry {
            rtx,
            cycles,
            size,
            fee,
            timestamp,
            ancestors_size: size,
            ancestors_fee: fee,
            ancestors_cycles: cycles,
            descendants_fee: fee,
            descendants_size: size,
            descendants_cycles: cycles,
            descendants_count: 1,
            ancestors_count: 1,
        }
    }
```

**File:** tx-pool/src/process.rs (L836-851)
```rust
            let mut tx_pool = self.tx_pool.write().await;

            _update_tx_pool_for_reorg(
                &mut tx_pool,
                &attached,
                &detached_headers,
                detached_proposal_id,
                snapshot,
                &self.callbacks,
                mine_mode,
            );

            // notice: readd_detached_tx don't update cache
            self.readd_detached_tx(&mut tx_pool, retain, fetched_cache)
                .await;
        }
```

**File:** tx-pool/src/process.rs (L904-906)
```rust
                {
                    let entry = TxEntry::new(rtx, verified.cycles, fee, tx_size);
                    if let Err(e) = _submit_entry(tx_pool, status, entry, &self.callbacks) {
```

**File:** tx-pool/src/process.rs (L1109-1113)
```rust
    // Remove expired transaction from pending
    tx_pool.remove_expired(callbacks);

    // Remove transactions from the pool until its size <= size_limit.
    let _ = tx_pool.limit_size(callbacks, None);
```

**File:** tx-pool/src/pool.rs (L271-288)
```rust
    pub(crate) fn remove_expired(&mut self, callbacks: &Callbacks) {
        let now_ms = ckb_systemtime::unix_time_as_millis();

        let removed: Vec<_> = self
            .pool_map
            .iter()
            .filter(|&entry| self.expiry + entry.inner.timestamp < now_ms)
            .map(|entry| entry.inner.clone())
            .collect();

        for entry in removed {
            let tx_hash = entry.transaction().hash();
            debug!("remove_expired {} timestamp({})", tx_hash, entry.timestamp);
            self.pool_map.remove_entry(&entry.proposal_short_id());
            let reject = Reject::Expiry(entry.timestamp);
            callbacks.call_reject(self, &entry, reject);
        }
    }
```

**File:** tx-pool/src/pool.rs (L343-353)
```rust
                let mut entries = self.pool_map.remove_entry_and_descendants(id);
                entries.sort_unstable_by_key(|entry| entry.ancestors_count);
                for mut entry in entries {
                    let tx_hash = entry.transaction().hash();
                    entry.reset_statistic_state();
                    let ret = self.add_pending(entry);
                    debug!(
                        "remove_by_detached_proposal from {:?} {} add_pending {:?}",
                        status, tx_hash, ret
                    );
                }
```

**File:** tx-pool/src/component/sort_key.rs (L76-104)
```rust
/// First compare fee_rate, select the smallest fee_rate,
/// and then select the latest timestamp, for eviction,
/// the latest timestamp which also means that the fewer descendants may exist.
#[derive(Eq, PartialEq, Clone, Debug)]
pub struct EvictKey {
    pub fee_rate: FeeRate,
    pub timestamp: u64,
    pub descendants_count: usize,
}

impl PartialOrd for EvictKey {
    fn partial_cmp(&self, other: &Self) -> Option<Ordering> {
        Some(self.cmp(other))
    }
}

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
}
```

**File:** tx-pool/src/component/pool_map.rs (L380-385)
```rust
    pub(crate) fn next_evict_entry(&self, status: Status) -> Option<ProposalShortId> {
        self.entries
            .iter_by_evict_key()
            .find(move |entry| entry.status == status)
            .map(|entry| entry.id.clone())
    }
```

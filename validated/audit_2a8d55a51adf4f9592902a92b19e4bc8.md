### Title
Global `expiry` Not Snapshotted Per Transaction Causes Retroactive Eviction Inconsistency - (File: `tx-pool/src/pool.rs`)

### Summary

CKB's tx-pool stores a single global `expiry` duration (derived from `config.expiry_hours`) that is applied uniformly to all pending transactions at eviction time. Because this value is not captured per-transaction at submission time, any runtime change to `expiry_hours` via the `update_tx_pool_config` RPC retroactively affects all already-pending transactions, causing inconsistent and unpredictable eviction behavior.

---

### Finding Description

`TxPool` holds a single `expiry` field set once at pool construction:

```rust
// tx-pool/src/pool.rs, line 57
let expiry = config.expiry_hours as u64 * 60 * 60 * 1000;
``` [1](#0-0) 

The `remove_expired` function, called on every reorg cycle, evicts transactions by comparing the **global** `self.expiry` against each entry's `timestamp` (the wall-clock time the transaction entered the pool):

```rust
// tx-pool/src/pool.rs, lines 271-288
pub(crate) fn remove_expired(&mut self, callbacks: &Callbacks) {
    let now_ms = ckb_systemtime::unix_time_as_millis();
    let removed: Vec<_> = self
        .pool_map
        .iter()
        .filter(|&entry| self.expiry + entry.inner.timestamp < now_ms)
        ...
``` [2](#0-1) 

`remove_expired` is invoked on every reorg update: [3](#0-2) 

The `update_tx_pool_config` RPC (confirmed present in `rpc/src/module/pool.rs`) allows a local RPC caller to change pool parameters at runtime, including `expiry_hours`. Because `self.expiry` is a single shared value — not snapshotted per transaction at submission time — any change to it immediately and retroactively alters the eviction deadline for **all** transactions already in the pool.

The `TxPoolConfig` struct confirms `expiry_hours` is a mutable runtime field: [4](#0-3) 

---

### Impact Explanation

**Impact: Medium**

- If `expiry_hours` is **reduced** (e.g., from 12 h to 1 h), all transactions that entered the pool more than 1 hour ago are immediately evicted on the next `remove_expired` sweep, even though they were submitted under the expectation of a 12-hour window. Legitimate transactions are silently dropped from the pool.
- If `expiry_hours` is **increased**, transactions that should have been evicted remain in the pool indefinitely, consuming memory and potentially being mined long after the submitter expected them to expire.
- In both cases, the submitter's expectation at submission time is violated by a post-hoc config change.

---

### Likelihood Explanation

**Likelihood: Medium**

The `update_tx_pool_config` RPC is a supported, documented local RPC. Any operator or script with local RPC access (the standard CKB operational model) can trigger this. The scenario is realistic during node maintenance, fee-rate tuning, or pool-size management. The bug is latent in every deployment and activates whenever `expiry_hours` is changed at runtime.

---

### Recommendation

Snapshot the effective expiry deadline per transaction at submission time. Add an `expiry_deadline_ms: u64` field to `TxEntry`, set at insertion as `unix_time_as_millis() + pool.expiry`. Change `remove_expired` to compare `now_ms > entry.inner.expiry_deadline_ms` instead of `self.expiry + entry.inner.timestamp < now_ms`. This ensures that a runtime change to `expiry_hours` only affects transactions submitted **after** the change, not those already in the pool.

---

### Proof of Concept

1. Submit a transaction `Tx_A` to the pool. Its `timestamp` is recorded as `T0`. The pool's `expiry` is `12 * 3600 * 1000` ms (12 hours). Expected eviction: `T0 + 12h`.
2. After 2 hours, call `update_tx_pool_config` with `expiry_hours = 1`.
3. On the next block arrival, `_update_tx_pool_for_reorg` calls `remove_expired`.
4. The check `self.expiry + entry.inner.timestamp < now_ms` becomes `(1h) + T0 < T0 + 2h` → `true`.
5. `Tx_A` is evicted immediately, 10 hours before the submitter expected.

The inverse also holds: reducing `expiry_hours` to 0 would evict the entire pool on the next reorg sweep regardless of when transactions were submitted. [2](#0-1) [5](#0-4)

### Citations

**File:** tx-pool/src/pool.rs (L46-57)
```rust
    pub(crate) expiry: u64,
    // conflicted transaction cache
    pub(crate) conflicts_cache: lru::LruCache<ProposalShortId, TransactionView>,
    // conflicted transaction outputs cache, input -> tx_short_id
    pub(crate) conflicts_outputs_cache: lru::LruCache<OutPoint, ProposalShortId>,
}

impl TxPool {
    /// Create new TxPool
    pub fn new(config: TxPoolConfig, snapshot: Arc<Snapshot>) -> TxPool {
        let recent_reject = Self::build_recent_reject(&config);
        let expiry = config.expiry_hours as u64 * 60 * 60 * 1000;
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

**File:** tx-pool/src/process.rs (L1109-1113)
```rust
    // Remove expired transaction from pending
    tx_pool.remove_expired(callbacks);

    // Remove transactions from the pool until its size <= size_limit.
    let _ = tx_pool.limit_size(callbacks, None);
```

**File:** util/app-config/src/configs/tx_pool.rs (L41-43)
```rust
    /// The expiration time for pool transactions in hours
    pub expiry_hours: u8,
}
```

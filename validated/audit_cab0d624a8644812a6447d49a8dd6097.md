### Title
`conflicts_outputs_cache` Not Cleaned Up on LRU Eviction from `conflicts_cache` — (File: `tx-pool/src/pool.rs`)

---

### Summary
`TxPool` maintains two coupled LRU caches for RBF conflict tracking: `conflicts_cache` (10,000 entries) and `conflicts_outputs_cache` (30,000 entries). When `conflicts_cache` silently evicts an entry due to capacity pressure, the corresponding input-keyed entries in `conflicts_outputs_cache` are never removed. Stale entries accumulate in `conflicts_outputs_cache` until it too fills up and begins evicting still-valid entries, permanently breaking RBF transaction recovery for legitimate users.

---

### Finding Description

`TxPool` declares two parallel LRU caches:

```rust
// conflicted transaction cache
pub(crate) conflicts_cache: lru::LruCache<ProposalShortId, TransactionView>,
// conflicted transaction outputs cache, input -> tx_short_id
pub(crate) conflicts_outputs_cache: lru::LruCache<OutPoint, ProposalShortId>,
``` [1](#0-0) 

They are sized at 10,000 and 30,000 entries respectively: [2](#0-1) 

`record_conflict` writes to **both** caches atomically: [3](#0-2) 

`remove_conflict` (the explicit removal path) correctly cleans **both** caches: [4](#0-3) 

However, the `lru::LruCache::put` call inside `record_conflict` silently evicts the least-recently-used entry from `conflicts_cache` when the cache is full. The `lru` crate provides no eviction callback. The evicted transaction's `OutPoint` keys remain permanently in `conflicts_outputs_cache`, which has no knowledge of the eviction.

The downstream consumer of these caches is `get_conflicted_txs_from_inputs`, called during RBF processing: [5](#0-4) 

It first looks up `conflicts_outputs_cache` for a `ProposalShortId`, then looks up `conflicts_cache` for the transaction. When `conflicts_cache` has evicted the entry but `conflicts_outputs_cache` still holds the stale `OutPoint → ProposalShortId` mapping, the second lookup returns `None` and the transaction is silently dropped from recovery.

The recovered transactions are pushed back to the verify queue for re-submission: [6](#0-5) 

Transactions whose entries were evicted from `conflicts_cache` are never re-queued.

---

### Impact Explanation

An attacker submitting conflicting transactions via the tx-pool RPC can drive `conflicts_cache` past its 10,000-entry limit. Each subsequent `record_conflict` call silently evicts one entry from `conflicts_cache` while leaving its `OutPoint` keys in `conflicts_outputs_cache`. Once `conflicts_outputs_cache` fills to 30,000 entries with stale mappings, it begins evicting still-valid entries. From that point forward, `get_conflicted_txs_from_inputs` returns incomplete results: transactions that were legitimately replaced by RBF and should be re-submitted to the pool are permanently lost from the recovery path. Affected users must manually rebroadcast their transactions.

---

### Likelihood Explanation

The attack requires submitting approximately 10,000 conflicting transactions to saturate `conflicts_cache`. Each conflicting transaction must reference an input already tracked in `pool_map.edges.inputs`, which is achievable by any unprivileged tx-pool submitter. The `conflicts_outputs_cache` is three times larger (30,000), but because each transaction can have multiple inputs, the stale-entry accumulation rate is higher than the eviction rate from `conflicts_cache`. No privileged access, key material, or majority hashpower is required.

---

### Recommendation

Use a custom eviction-aware wrapper around `conflicts_cache` that, on LRU eviction, iterates the evicted transaction's inputs and removes the corresponding entries from `conflicts_outputs_cache`. Alternatively, replace the two separate LRU caches with a single structure keyed by `ProposalShortId` that stores both the `TransactionView` and its input `OutPoint` set, so eviction is always atomic.

---

### Proof of Concept

1. Attacker connects to a CKB node with RBF enabled (`min_rbf_rate > min_fee_rate`).
2. Attacker submits 10,001 transactions, each conflicting with a distinct in-pool transaction, triggering `record_conflict` for each. After 10,001 calls, `conflicts_cache` has evicted its first entry; `conflicts_outputs_cache` still holds that entry's `OutPoint` keys.
3. Victim submits transaction V that spends an input previously held by the evicted conflict entry.
4. A subsequent RBF replacement of V calls `process_rbf`, which calls `get_conflicted_txs_from_inputs` with V's inputs.
5. `conflicts_outputs_cache.peek(&input)` returns the stale `ProposalShortId`; `conflicts_cache.peek(id)` returns `None` (evicted). The victim's original transaction is not added to `may_recovered_txs`.
6. The victim's transaction is permanently absent from the verify queue and must be manually rebroadcast. [7](#0-6) [8](#0-7)

### Citations

**File:** tx-pool/src/pool.rs (L31-32)
```rust
const CONFLICTES_CACHE_SIZE: usize = 10_000;
const CONFLICTES_INPUTS_CACHE_SIZE: usize = 30_000;
```

**File:** tx-pool/src/pool.rs (L47-51)
```rust
    // conflicted transaction cache
    pub(crate) conflicts_cache: lru::LruCache<ProposalShortId, TransactionView>,
    // conflicted transaction outputs cache, input -> tx_short_id
    pub(crate) conflicts_outputs_cache: lru::LruCache<OutPoint, ProposalShortId>,
}
```

**File:** tx-pool/src/pool.rs (L164-188)
```rust
    pub(crate) fn record_conflict(&mut self, tx: TransactionView) {
        let short_id = tx.proposal_short_id();
        for inputs in tx.input_pts_iter() {
            self.conflicts_outputs_cache.put(inputs, short_id.clone());
        }
        self.conflicts_cache.put(short_id.clone(), tx);
        debug!(
            "record_conflict {:?} now cache size: {}",
            short_id,
            self.conflicts_cache.len()
        );
    }

    pub(crate) fn remove_conflict(&mut self, short_id: &ProposalShortId) {
        if let Some(tx) = self.conflicts_cache.pop(short_id) {
            for inputs in tx.input_pts_iter() {
                self.conflicts_outputs_cache.pop(&inputs);
            }
        }
        debug!(
            "remove_conflict {:?} now cache size: {}",
            short_id,
            self.conflicts_cache.len()
        );
    }
```

**File:** tx-pool/src/pool.rs (L190-201)
```rust
    pub(crate) fn get_conflicted_txs_from_inputs(
        &self,
        inputs: impl Iterator<Item = OutPoint>,
    ) -> Vec<TransactionView> {
        inputs
            .filter_map(|input| {
                self.conflicts_outputs_cache
                    .peek(&input)
                    .and_then(|id| self.conflicts_cache.peek(id).cloned())
            })
            .collect()
    }
```

**File:** tx-pool/src/process.rs (L154-163)
```rust
                if !may_recovered_txs.is_empty() {
                    let self_clone = self.clone();
                    tokio::spawn(async move {
                        // push the recovered txs back to verify queue, so that they can be verified and submitted again
                        let mut queue = self_clone.verify_queue.write().await;
                        for tx in may_recovered_txs {
                            debug!("recover back: {:?}", tx.proposal_short_id());
                            let _ = queue.add_tx(tx, false, None);
                        }
                    });
```

**File:** tx-pool/src/process.rs (L203-234)
```rust
        let all_removed: Vec<_> = conflicts
            .iter()
            .flat_map(|id| tx_pool.pool_map.remove_entry_and_descendants(id))
            .collect();

        available_inputs.extend(
            all_removed
                .iter()
                .flat_map(|removed| removed.transaction().input_pts_iter()),
        );

        for input in entry.transaction().input_pts_iter() {
            available_inputs.remove(&input);
        }

        may_recovered_txs = tx_pool.get_conflicted_txs_from_inputs(available_inputs.into_iter());
        for old in all_removed {
            debug!(
                "remove conflict tx {} for RBF by new tx {}",
                old.transaction().hash(),
                entry.transaction().hash()
            );
            let reject =
                Reject::RBFRejected(format!("replaced by tx {}", entry.transaction().hash()));

            // RBF replace successfully, put old transactions into conflicts pool
            tx_pool.record_conflict(old.transaction().clone());
            // after removing old tx from tx_pool, we call reject callbacks manually
            self.callbacks.call_reject(tx_pool, &old, reject);
        }
        assert!(!may_recovered_txs.contains(entry.transaction()));
        may_recovered_txs
```

### Title
`remove_expired()` Removes Expired Parent Transactions Without Evicting Dependent Child Transactions, Leaving Permanently-Invalid Entries in the Tx-Pool - (File: tx-pool/src/pool.rs)

### Summary

`TxPool::remove_expired()` in `tx-pool/src/pool.rs` removes each expired transaction individually via `pool_map.remove_entry()`, but does **not** remove descendant transactions that depend on the expired parent's outputs. These child transactions remain in the pool permanently invalid — their inputs reference outputs that no longer exist in the pool or on-chain — consuming pool capacity and blocking legitimate transactions.

### Finding Description

`remove_expired()` iterates over all pool entries whose timestamp has exceeded the configured expiry window and removes each one using `pool_map.remove_entry()`:

```rust
// tx-pool/src/pool.rs lines 271-288
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
        self.pool_map.remove_entry(&entry.proposal_short_id());   // ← single-entry removal only
        let reject = Reject::Expiry(entry.timestamp);
        callbacks.call_reject(self, &entry, reject);
    }
}
``` [1](#0-0) 

`pool_map.remove_entry()` removes only the single entry, updates ancestor/descendant index keys, and cleans up the removed entry's own input edges — but it does **not** remove child transactions that spend the removed entry's outputs:

```rust
// tx-pool/src/component/pool_map.rs lines 235-250
pub(crate) fn remove_entry(&mut self, id: &ProposalShortId) -> Option<TxEntry> {
    self.entries.remove_by_id(id).map(|entry| {
        self.update_ancestors_index_key(&entry.inner, EntryOp::Remove);
        self.update_descendants_index_key(&entry.inner, EntryOp::Remove);
        self.remove_entry_edges(&entry.inner);   // removes parent's own inputs, NOT children's inputs
        self.remove_entry_links(id);
        ...
        entry.inner
    })
}
``` [2](#0-1) 

`remove_entry_edges` only removes the expired entry's own inputs from `edges.inputs` — it does not touch the child transactions' input edges that reference the expired parent's outputs:

```rust
// tx-pool/src/component/pool_map.rs lines 642-653
fn remove_entry_edges(&mut self, entry: &TxEntry) {
    for i in entry.transaction().input_pts_iter() {
        self.edges.remove_input(&i);   // removes parent's inputs only
    }
    ...
}
``` [3](#0-2) 

By contrast, every other removal path that handles eviction correctly uses `remove_entry_and_descendants()`:

- `limit_size()` uses `pool_map.remove_entry_and_descendants(&id)` [4](#0-3) 
- `remove_tx()` uses `pool_map.remove_entry_and_descendants(id)` [5](#0-4) 

`remove_entry_and_descendants()` correctly collects all descendants before removal:

```rust
pub(crate) fn remove_entry_and_descendants(&mut self, id: &ProposalShortId) -> Vec<TxEntry> {
    let mut removed_ids = vec![id.to_owned()];
    removed_ids.extend(self.calc_descendants(id));
    ...
}
``` [6](#0-5) 

### Impact Explanation

After a parent transaction expires and is removed via `remove_expired()`, any child transactions that spend the parent's outputs remain in the pool. These children:

1. Reference outputs that are neither live on-chain (parent was never committed) nor present in the pool (parent was removed).
2. Can never be committed to a block — they are permanently invalid.
3. Continue to count toward `total_tx_size` and `total_tx_cycles`, consuming pool capacity.
4. Remain until they themselves expire or are evicted by `limit_size` under pool pressure.

An attacker who submits many parent→child transaction chains and waits for the expiry window can fill the pool with permanently-invalid child entries, degrading pool capacity for legitimate users. The pool's own comment on `remove_expired` says "Expire all transaction (and their dependencies)" — the implementation does not match this intent. [7](#0-6) 

### Likelihood Explanation

The attack is reachable by any unprivileged RPC caller via `send_transaction`. The attacker submits a parent transaction and one or more child transactions spending the parent's outputs. After `expiry_hours` (configurable, default 12 hours), the parent expires. `remove_expired()` is called during every reorg/block-processing cycle via `_update_tx_pool_for_reorg`:

```rust
// tx-pool/src/process.rs line 1110
tx_pool.remove_expired(callbacks);
``` [8](#0-7) 

No privileged access, key material, or majority hashpower is required. The attacker only needs to submit valid transactions and wait. The attack is slow (requires waiting for expiry) but requires no special capability beyond normal RPC access.

### Recommendation

Replace `pool_map.remove_entry()` with `pool_map.remove_entry_and_descendants()` inside `remove_expired()`, consistent with how `limit_size()` and `remove_tx()` handle removal. All evicted descendants should also have `callbacks.call_reject()` invoked so callers are notified:

```rust
pub(crate) fn remove_expired(&mut self, callbacks: &Callbacks) {
    let now_ms = ckb_systemtime::unix_time_as_millis();
    let expired_ids: Vec<_> = self
        .pool_map
        .iter()
        .filter(|&entry| self.expiry + entry.inner.timestamp < now_ms)
        .map(|entry| entry.inner.proposal_short_id())
        .collect();

    for id in expired_ids {
        let removed = self.pool_map.remove_entry_and_descendants(&id);
        for entry in removed {
            let reject = Reject::Expiry(entry.timestamp);
            callbacks.call_reject(self, &entry, reject);
        }
    }
}
```

### Proof of Concept

1. Submit parent transaction `P` that creates output `O` (via RPC `send_transaction`).
2. Submit child transaction `C` that spends `O` as input (via RPC `send_transaction`).
3. Both `P` and `C` are now in the pending pool.
4. Wait `expiry_hours` (e.g., 12 hours by default).
5. A new block arrives; `_update_tx_pool_for_reorg` → `remove_expired()` is called.
6. `P` is expired and removed via `remove_entry()`. `C` is **not** removed.
7. Query `tx_pool_info`: `C` still appears in the pool with `pending.value() == 1`, consuming `total_tx_size`.
8. `C` can never be committed (its input `O` is not a live cell). It is permanently stuck until it expires or pool pressure triggers `limit_size` eviction.
9. Repeat steps 1–8 at scale to exhaust pool capacity. [1](#0-0) [9](#0-8)

### Citations

**File:** tx-pool/src/pool.rs (L270-288)
```rust
    // Expire all transaction (and their dependencies) in the pool.
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

**File:** tx-pool/src/pool.rs (L306-307)
```rust
            if let Some(id) = next_evict_entry() {
                let removed = self.pool_map.remove_entry_and_descendants(&id);
```

**File:** tx-pool/src/pool.rs (L358-360)
```rust
    pub(crate) fn remove_tx(&mut self, id: &ProposalShortId) -> bool {
        let entries = self.pool_map.remove_entry_and_descendants(id);
        !entries.is_empty()
```

**File:** tx-pool/src/component/pool_map.rs (L235-265)
```rust
    pub(crate) fn remove_entry(&mut self, id: &ProposalShortId) -> Option<TxEntry> {
        self.entries.remove_by_id(id).map(|entry| {
            debug!(
                "remove entry {} from status: {:?}",
                entry.inner.transaction().hash(),
                entry.status
            );
            self.update_ancestors_index_key(&entry.inner, EntryOp::Remove);
            self.update_descendants_index_key(&entry.inner, EntryOp::Remove);
            self.remove_entry_edges(&entry.inner);
            self.remove_entry_links(id);
            self.track_entry_statics(Some(entry.status), None);
            self.update_stat_for_remove_tx(entry.inner.size, entry.inner.cycles);
            entry.inner
        })
    }

    pub(crate) fn remove_entry_and_descendants(&mut self, id: &ProposalShortId) -> Vec<TxEntry> {
        let mut removed_ids = vec![id.to_owned()];
        removed_ids.extend(self.calc_descendants(id));

        // update links state for remove, so that we won't update_descendants_index_key in remove_entry
        for id in &removed_ids {
            self.remove_entry_links(id);
        }

        removed_ids
            .iter()
            .filter_map(|id| self.remove_entry(id))
            .collect()
    }
```

**File:** tx-pool/src/component/pool_map.rs (L642-653)
```rust
    fn remove_entry_edges(&mut self, entry: &TxEntry) {
        for i in entry.transaction().input_pts_iter() {
            // release input record
            self.edges.remove_input(&i);
        }
        let id = entry.proposal_short_id();
        for d in entry.related_dep_out_points().cloned() {
            self.edges.delete_txid_by_dep(d, &id);
        }

        self.edges.header_deps.remove(&id);
    }
```

**File:** tx-pool/src/process.rs (L1109-1113)
```rust
    // Remove expired transaction from pending
    tx_pool.remove_expired(callbacks);

    // Remove transactions from the pool until its size <= size_limit.
    let _ = tx_pool.limit_size(callbacks, None);
```

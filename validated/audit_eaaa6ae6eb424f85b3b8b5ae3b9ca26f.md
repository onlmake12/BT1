### Title
`remove_expired` Removes Parent Transactions Without Evicting Dependent Descendants, Leaving Zombie Entries in the Pool - (File: tx-pool/src/pool.rs)

### Summary

`TxPool::remove_expired` iterates over all expired entries and removes each one individually via `pool_map.remove_entry(...)`. It does **not** call `remove_entry_and_descendants`. As a result, any child transactions that were submitted after the parent (and therefore have not yet crossed the expiry threshold themselves) remain in the pool in a permanently uncommittable state, consuming pool capacity and locking their input outpoints in `edges.inputs`.

### Finding Description

The function comment explicitly states the intended semantics:

```rust
// Expire all transaction (and their dependencies) in the pool.
pub(crate) fn remove_expired(&mut self, callbacks: &Callbacks) {
```

But the implementation only removes the single expired entry:

```rust
self.pool_map.remove_entry(&entry.proposal_short_id());
```

`remove_entry` (pool_map.rs:235-250) does the following for the removed entry:
- Calls `remove_entry_edges` — releases the **parent's own** inputs from `edges.inputs` and its dep-references.
- Calls `remove_entry_links` — severs the parent↔child link in `links`, so the child's `links.parents` no longer contains the parent.
- Updates ancestor/descendant score keys for the remaining entries.

What it does **not** do is remove the child transactions. After the parent is evicted:

1. The child transactions remain in `entries` with `Status::Pending/Gap/Proposed`.
2. Their inputs (which reference the now-gone parent's output outpoints) remain registered in `edges.inputs`.
3. Their `links.parents` is now empty (the link was severed), so `calc_ancestors` returns an empty set — the pool believes they are root transactions.
4. They can never be committed: the parent's outputs are neither in the pool nor on-chain (the parent was never mined; it expired).
5. They will not be re-evicted until their own individual timestamps also cross the expiry threshold.

Contrast this with every other removal path that is aware of the dependency graph:

- `limit_size` → `remove_entry_and_descendants`
- `remove_tx` (RPC) → `remove_entry_and_descendants`
- `remove_by_detached_proposal` → `remove_entry_and_descendants`
- `resolve_conflict` → `remove_entry_and_descendants`
- `resolve_conflict_header_dep` → `remove_entry_and_descendants`

`remove_expired` is the only removal path that uses the single-entry variant.

### Impact Explanation

**Pool resource exhaustion / zombie entry accumulation.** An attacker who controls transaction submission can:

1. Submit a low-fee parent transaction `P` (timestamp `T`).
2. Submit `N` child transactions `C₁…Cₙ` that spend `P`'s outputs, each submitted at time `T + δ` (where `δ` is small but positive).
3. Wait for `expiry_hours` to elapse. `P` expires first (at `T + expiry`). `remove_expired` removes `P` but leaves `C₁…Cₙ` in the pool.
4. `C₁…Cₙ` remain in the pool for an additional `δ` time, consuming `pool_map.total_tx_size` and `total_tx_cycles` budget, and holding their input outpoints in `edges.inputs`.

During the window `[T + expiry, T + expiry + δ]`:
- The pool's size/cycle budget is partially consumed by unspendable entries.
- Any new transaction that attempts to spend the same outpoints as `C₁…Cₙ` is rejected as a double-spend (because `edges.inputs` still records those outpoints as claimed), even though the claiming transactions can never be mined.
- If `δ` is large (e.g., the attacker submits children just before the parent expires), the zombie window approaches a full `expiry_hours` period.

By repeating this pattern with many parent/child chains, an attacker can keep a significant fraction of the pool filled with permanently unspendable transactions, degrading throughput for legitimate users.

### Likelihood Explanation

The attack requires only the ability to submit transactions to the pool — available to any unprivileged peer or RPC caller. The cost is the transaction fees for the parent and child transactions. Because the parent is intentionally low-fee (to expire quickly), the cost is minimal. The `expiry_hours` configuration (default: 12 hours) gives a long window for the zombie children to occupy pool space.

### Recommendation

Replace `remove_entry` with `remove_entry_and_descendants` inside `remove_expired`, consistent with every other eviction path:

```rust
// tx-pool/src/pool.rs  remove_expired()
for entry in removed {
    let tx_hash = entry.transaction().hash();
    debug!("remove_expired {} timestamp({})", tx_hash, entry.timestamp);
-   self.pool_map.remove_entry(&entry.proposal_short_id());
+   let evicted = self.pool_map.remove_entry_and_descendants(&entry.proposal_short_id());
    let reject = Reject::Expiry(entry.timestamp);
-   callbacks.call_reject(self, &entry, reject);
+   for e in evicted {
+       callbacks.call_reject(self, &e, reject.clone());
+   }
}
```

This aligns the implementation with the existing comment ("and their dependencies") and with the behaviour of all other eviction paths.

### Proof of Concept

1. Submit parent transaction `P` with fee just above the minimum (so it is accepted but expires after `expiry_hours`).
2. Submit child transaction `C` spending an output of `P`, submitted slightly later.
3. Advance the node clock (or wait) past `expiry_hours` from `P`'s submission time but before `expiry_hours` from `C`'s submission time.
4. Observe that `tx_pool_info` shows `C` still in the pending pool even though `P` is gone.
5. Attempt to submit a new transaction `C'` spending the same output of `P` as `C` — it is rejected with a double-spend error, even though `P` is no longer in the pool and was never mined.

**Root cause references:** [1](#0-0) [2](#0-1) [3](#0-2)

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

**File:** tx-pool/src/process.rs (L440-456)
```rust
    pub(crate) async fn remove_tx(&self, tx_hash: Byte32) -> bool {
        let id = ProposalShortId::from_tx_hash(&tx_hash);
        {
            let mut queue = self.verify_queue.write().await;
            if queue.remove_tx(&id).is_some() {
                return true;
            }
        }
        {
            let mut orphan = self.orphan.write().await;
            if orphan.remove_orphan_tx(&id).is_some() {
                return true;
            }
        }
        let mut tx_pool = self.tx_pool.write().await;
        tx_pool.remove_tx(&id)
    }
```

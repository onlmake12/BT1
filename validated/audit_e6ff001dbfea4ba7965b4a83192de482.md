Audit Report

## Title
`remove_expired` Removes Parent Transactions Without Evicting Dependent Descendants, Leaving Zombie Entries in the Pool - (File: tx-pool/src/pool.rs)

## Summary

`TxPool::remove_expired` calls `pool_map.remove_entry(...)` at line 284, which removes only the single expired entry. Child transactions that depend on the expired parent remain in the pool in a permanently uncommittable state, consuming pool capacity and holding their input outpoints locked. This contradicts the function's own comment at line 270 ("Expire all transaction **and their dependencies** in the pool") and diverges from every other eviction path in the codebase, all of which use `remove_entry_and_descendants`.

## Finding Description

**Root cause — `tx-pool/src/pool.rs` line 284:**

`remove_expired` iterates over expired entries and calls `self.pool_map.remove_entry(&entry.proposal_short_id())` for each one. [1](#0-0) 

`remove_entry` (pool_map.rs:235–250) removes only the targeted entry: it calls `remove_entry_edges` and `remove_entry_links` for that entry alone, with no traversal of descendants. [2](#0-1) 

`remove_entry_and_descendants` (pool_map.rs:252–265) first calls `calc_descendants` to collect all transitive children, pre-severs all links, then removes each entry — the correct behavior. [3](#0-2) 

**Every other eviction path uses the correct variant:**
- `limit_size` → `remove_entry_and_descendants` (pool.rs:307)
- `remove_by_detached_proposal` → `remove_entry_and_descendants` (pool.rs:343)
- `remove_tx` → `remove_entry_and_descendants` (pool.rs:359) [4](#0-3) [5](#0-4) [6](#0-5) 

**Post-eviction zombie state of children:**
1. They remain in `entries` with `Status::Pending/Gap/Proposed`.
2. Their inputs (spending the now-gone parent's outputs) remain registered in `edges.inputs`.
3. `remove_entry_links` was called on the parent, so the child's `links.parents` is now empty — the pool treats them as root transactions, but their inputs reference outputs that are neither on-chain nor in the pool.
4. Any new transaction attempting to spend the same outpoints is rejected as a double-spend, even though the original claimant can never be mined.
5. The zombies persist until their own individual timestamps cross the expiry threshold.

**Exploit amplification:** An attacker submits children at time `T + expiry_hours − ε` (just before the parent expires). The children's expiry is `T + 2·expiry_hours − ε`, giving a zombie window of nearly one full `expiry_hours` period (default: 12 hours). By chaining many parent/child groups, the attacker can keep a large fraction of the pool filled with permanently unspendable entries.

## Impact Explanation

**High (10001–15000 points) — Vulnerabilities or bad designs which could cause CKB network congestion with few costs.**

During the zombie window, pool size and cycle budgets are partially consumed by unspendable entries, reducing capacity for legitimate transactions. Legitimate transactions spending the same outpoints as zombie children are rejected with a false double-spend error. By repeating the pattern across many parent/child chains, an attacker can sustain pool congestion for up to `expiry_hours` per cycle with minimal ongoing cost.

## Likelihood Explanation

The attack requires only the ability to submit transactions — available to any unprivileged peer or RPC caller. The parent transaction is intentionally low-fee (accepted but expires quickly). Children are submitted just before the parent's expiry to maximize the zombie window. No special privileges, leaked keys, or victim mistakes are required. The attack is repeatable and cheap to sustain.

## Recommendation

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

This aligns the implementation with the existing comment and with the behavior of all other eviction paths.

## Proof of Concept

1. Submit parent transaction `P` with a low fee (above minimum, so it is accepted). Record submission timestamp `T`.
2. At time `T + expiry_hours − ε`, submit child transaction `C` spending an output of `P`. `C` is accepted because `P` is still in the pool.
3. Wait until time `T + expiry_hours`. `remove_expired` fires, removes `P` via `remove_entry`, but leaves `C` in the pool.
4. Query `tx_pool_info` — `C` is still listed as pending even though `P` is gone and was never mined.
5. Attempt to submit a new transaction `C'` spending the same output of `P` as `C` — it is rejected with a double-spend error, despite `P` being absent from both the pool and the chain.
6. `C` remains in the pool until `T + 2·expiry_hours − ε`, consuming pool capacity and blocking `C'` for nearly a full `expiry_hours` window.
7. Repeat with many parent/child groups to amplify pool congestion.

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

**File:** tx-pool/src/pool.rs (L306-308)
```rust
            if let Some(id) = next_evict_entry() {
                let removed = self.pool_map.remove_entry_and_descendants(&id);
                for entry in removed {
```

**File:** tx-pool/src/pool.rs (L343-344)
```rust
                let mut entries = self.pool_map.remove_entry_and_descendants(id);
                entries.sort_unstable_by_key(|entry| entry.ancestors_count);
```

**File:** tx-pool/src/pool.rs (L358-360)
```rust
    pub(crate) fn remove_tx(&mut self, id: &ProposalShortId) -> bool {
        let entries = self.pool_map.remove_entry_and_descendants(id);
        !entries.is_empty()
```

**File:** tx-pool/src/component/pool_map.rs (L235-250)
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
```

**File:** tx-pool/src/component/pool_map.rs (L252-265)
```rust
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

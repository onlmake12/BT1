### Title
Stale `descendants_*` Accounting in `remove_entry_and_descendants` Leaves Ancestor Entries with Inflated Eviction Priority After RBF Replacement — (File: `tx-pool/src/component/pool_map.rs`)

---

### Summary

When `remove_entry_and_descendants` removes a transaction subtree from the tx-pool (e.g., during RBF replacement), it pre-removes all link entries before calling `remove_entry` on each node. Because `update_ancestors_index_key` relies on those same links to find which still-pooled ancestors to update, it silently finds no ancestors and skips the `sub_descendant_weight` call. The result is that every ancestor of the removed subtree root retains permanently inflated `descendants_fee`, `descendants_size`, `descendants_cycles`, and `descendants_count` fields. This is the direct CKB analog of the MetaManager `reStake` bug: a user-initiated reversal of a prior action (RBF replacing a child tx ≈ restaking) fails to undo the intermediate accounting state (descendants accumulation ≈ slash).

---

### Finding Description

**Root cause — `pool_map.rs` `remove_entry_and_descendants`:**

```rust
// tx-pool/src/component/pool_map.rs  lines 252-265
pub(crate) fn remove_entry_and_descendants(&mut self, id: &ProposalShortId) -> Vec<TxEntry> {
    let mut removed_ids = vec![id.to_owned()];
    removed_ids.extend(self.calc_descendants(id));

    // update links state for remove, so that we won't update_descendants_index_key in remove_entry
    for id in &removed_ids {
        self.remove_entry_links(id);   // ← ALL links torn down here
    }

    removed_ids
        .iter()
        .filter_map(|id| self.remove_entry(id))   // ← update_ancestors_index_key called here
        .collect()
}
```

`remove_entry` then calls `update_ancestors_index_key`:

```rust
// tx-pool/src/component/pool_map.rs  lines 432-445
fn update_ancestors_index_key(&mut self, child: &TxEntry, op: EntryOp) {
    let ancestors: HashSet<ProposalShortId> =
        self.links.calc_ancestors(&child.proposal_short_id());  // ← returns ∅ because links already gone
    for anc_id in &ancestors {
        self.entries.modify_by_id(anc_id, |e| {
            match op {
                EntryOp::Remove => e.inner.sub_descendant_weight(child),  // ← never reached
                ...
            };
            e.evict_key = e.inner.as_evict_key();
        });
    }
}
```

Because `remove_entry_links` was already called for every node in the subtree, `calc_ancestors` returns an empty set for the subtree root. The still-pooled ancestors of that root never receive `sub_descendant_weight`, so their `descendants_fee / size / cycles / count` remain at the pre-removal values.

**Contrast with the single-entry path:** When `remove_entry` is called directly (not via `remove_entry_and_descendants`), links are still intact at the time `update_ancestors_index_key` runs, so ancestors are found and correctly updated. The bug is exclusive to the batch-removal path.

**Affected call sites** (all invoke `remove_entry_and_descendants`):

| Call site | Trigger |
|---|---|
| `process_rbf` (`process.rs:203`) | RBF replacement — attacker-controlled |
| `resolve_conflict` (`pool_map.rs:310`) | Committed tx evicts pool descendants |
| `resolve_conflict_header_dep` (`pool_map.rs:285`) | Detached header evicts pool txs |
| `limit_size` (`pool.rs:307`) | Pool-full eviction |
| `check_and_record_ancestors` (`pool_map.rs:618`) | Ancestor-limit eviction |
| `remove_by_detached_proposal` (`pool.rs:343`) | Proposal detachment + re-add (double-counts) |

The RBF path is the most attacker-controlled and directly analogous to the MetaManager restaking scenario.

---

### Impact Explanation

`descendants_*` fields feed directly into `EvictKey`:

```rust
// tx-pool/src/component/entry.rs  lines 234-247
impl From<&TxEntry> for EvictKey {
    fn from(entry: &TxEntry) -> Self {
        let descendants_feerate = FeeRate::calculate(entry.descendants_fee, descendants_weight);
        let feerate = FeeRate::calculate(entry.fee, weight);
        EvictKey {
            fee_rate: descendants_feerate.max(feerate),  // ← inflated by stale descendants_fee
            ...
        }
    }
}
```

`next_evict_entry` picks the entry with the **lowest** `EvictKey` to evict. An inflated `descendants_feerate` raises `fee_rate`, making the parent tx **less likely to be evicted** than it should be.

Concrete consequences:
1. **Eviction-resistance**: A low-fee parent tx can be made to appear as if it has high-fee descendants, shielding it from pool eviction even when the pool is full.
2. **Pool-stuffing**: An attacker can keep a low-fee anchor tx in the pool indefinitely by repeatedly RBF-replacing its children, each replacement adding another ghost `descendants_fee` increment to the parent.
3. **Fairness violation**: Legitimate high-fee txs may be evicted in preference to the attacker's artificially-boosted parent tx, degrading mining revenue and tx throughput for honest users.
4. **Double-counting on re-add**: In `remove_by_detached_proposal`, removed txs are re-added via `add_pending`, which calls `record_entry_descendants` → `update_ancestors_index_key(Add)`. Since the prior `Remove` was skipped, the ancestor's `descendants_*` is incremented a second time for the same child.

---

### Likelihood Explanation

- **Attacker entry path**: Any unprivileged tx-pool submitter. The attacker submits a parent tx (tx0) and a child tx (tx1), then RBF-replaces tx1 with tx2 (higher fee, same input). This is a standard, documented RBF flow reachable via the public `send_transaction` RPC.
- **RBF availability**: RBF is enabled when `min_rbf_rate > min_fee_rate` in node config. This is an operator-level setting, but nodes that enable RBF (which is the intended production use) are fully exposed.
- **Cost**: Each RBF round costs the attacker the fee delta (`min_rbf_rate × size`). The ghost increment per round equals the replaced tx's fee. With a small fee delta, the attacker can accumulate large ghost `descendants_fee` cheaply.
- **Normal-operation trigger**: Even without a deliberate attacker, `resolve_conflict` fires on every committed block that evicts pool descendants, silently inflating ancestors' `descendants_*` during routine operation.

---

### Recommendation

Before tearing down links in `remove_entry_and_descendants`, explicitly update the ancestors of the subtree root:

```rust
pub(crate) fn remove_entry_and_descendants(&mut self, id: &ProposalShortId) -> Vec<TxEntry> {
    let mut removed_ids = vec![id.to_owned()];
    removed_ids.extend(self.calc_descendants(id));

+   // Update still-pooled ancestors of the root BEFORE links are removed,
+   // so calc_ancestors can still traverse them.
+   if let Some(root_entry) = self.get(id) {
+       let root_entry = root_entry.clone();
+       self.update_ancestors_index_key(&root_entry, EntryOp::Remove);
+   }

    for id in &removed_ids {
        self.remove_entry_links(id);
    }

    removed_ids
        .iter()
        .filter_map(|id| self.remove_entry(id))
        .collect()
}
```

Alternatively, refactor `remove_entry` to accept a flag that skips the link-based ancestor/descendant update when called from the batch path, and handle ancestor updates explicitly before link teardown.

---

### Proof of Concept

**Setup** (RBF enabled, `min_rbf_rate > min_fee_rate`):

1. Submit **tx0** spending a confirmed UTXO. tx0 enters the pool with `descendants_fee = tx0.fee`, `descendants_count = 1`.
2. Submit **tx1** spending tx0's output (tx0 is ancestor). tx0's `descendants_fee` becomes `tx0.fee + tx1.fee`, `descendants_count = 2`.
3. Submit **tx2** (RBF replacement of tx1): same input as tx1, fee > tx1.fee + `min_rbf_rate × size`. tx1 and its descendants are removed via `remove_entry_and_descendants`. Due to the bug, tx0's `descendants_fee` is **not decremented** — it stays at `tx0.fee + tx1.fee`.
4. tx2 is inserted as a new child of tx0. tx0's `descendants_fee` is incremented by tx2.fee → now `tx0.fee + tx1.fee + tx2.fee` (correct value: `tx0.fee + tx2.fee`).
5. Repeat steps 2–4 with tx3, tx4, … Each round adds another ghost `txN.fee` to tx0's `descendants_fee`.
6. When the pool is full and eviction runs, tx0's `EvictKey.fee_rate` is computed from the inflated `descendants_fee`, making tx0 appear more valuable than it is and shielding it from eviction.

**Relevant code locations:** [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4) [6](#0-5) [7](#0-6)

### Citations

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

**File:** tx-pool/src/component/pool_map.rs (L432-445)
```rust
    fn update_ancestors_index_key(&mut self, child: &TxEntry, op: EntryOp) {
        let ancestors: HashSet<ProposalShortId> =
            self.links.calc_ancestors(&child.proposal_short_id());
        for anc_id in &ancestors {
            // update parent score
            self.entries.modify_by_id(anc_id, |e| {
                match op {
                    EntryOp::Remove => e.inner.sub_descendant_weight(child),
                    EntryOp::Add => e.inner.add_descendant_weight(child),
                };
                e.evict_key = e.inner.as_evict_key();
            });
        }
    }
```

**File:** tx-pool/src/component/entry.rs (L132-142)
```rust
    /// Update ancestor state for remove an entry
    pub fn sub_descendant_weight(&mut self, entry: &TxEntry) {
        self.descendants_count = self.descendants_count.saturating_sub(1);
        self.descendants_size = self.descendants_size.saturating_sub(entry.size);
        self.descendants_cycles = self.descendants_cycles.saturating_sub(entry.cycles);
        self.descendants_fee = Capacity::shannons(
            self.descendants_fee
                .as_u64()
                .saturating_sub(entry.fee.as_u64()),
        );
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

**File:** tx-pool/src/process.rs (L190-235)
```rust
    fn process_rbf(
        &self,
        tx_pool: &mut TxPool,
        entry: &TxEntry,
        conflicts: &HashSet<ProposalShortId>,
    ) -> Vec<TransactionView> {
        let mut may_recovered_txs = vec![];
        let mut available_inputs = HashSet::new();

        if conflicts.is_empty() {
            return may_recovered_txs;
        }

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
    }
```

**File:** tx-pool/src/pool.rs (L290-329)
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
    }
```

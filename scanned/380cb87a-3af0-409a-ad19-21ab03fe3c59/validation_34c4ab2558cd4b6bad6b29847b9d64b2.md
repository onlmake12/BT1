Audit Report

## Title
`remove_expired` Leaves Descendant Transactions Orphaned in Pool with Stale Inputs — (File: `tx-pool/src/pool.rs`)

## Summary
`remove_expired` in `tx-pool/src/pool.rs` calls `pool_map.remove_entry` for each expired transaction instead of `pool_map.remove_entry_and_descendants`. When a parent transaction expires before its child, the child remains in `entries` with its `edges.inputs` records intact, pointing to outputs that no longer exist on-chain or in the pool. This creates a persistent state inconsistency that enables false double-spend rejection of legitimate transactions and corrupts block templates for miners.

## Finding Description

`PoolMap` maintains three tightly coupled structures: `entries` (canonical tx set), `edges` (input/dep/output relationships), and `links` (parent/child relationships).

`remove_expired` collects all entries whose `timestamp + expiry < now_ms` and removes each individually: [1](#0-0) 

The filter is timestamp-based. If `tx_A` (parent) was submitted at time T and `tx_B` (child spending `tx_A`'s output `O1`) was submitted at T+1 minute, then `tx_A` expires at T+expiry and `tx_B` expires at T+1min+expiry. When `remove_expired` fires at T+expiry, only `tx_A` is in the `removed` list. `tx_A` is removed via `remove_entry`: [2](#0-1) 

`remove_entry_edges` cleans up `tx_A`'s own edges (its inputs, deps, header deps). `remove_entry_links` removes `tx_A` as a parent from `tx_B`'s link record. However, `tx_B`'s own edge — `edges.inputs[O1] = tx_B` — is never touched, because `remove_entry_edges` only removes the edges belonging to the transaction being removed, not those of its descendants. [3](#0-2) 

After `tx_A` is removed, `O1` does not exist anywhere (not on-chain, not in the pool), yet `edges.inputs[O1] = tx_B` persists. `tx_B` remains in `entries` in whatever status it held (Pending, Gap, or Proposed).

Every other removal path uses `remove_entry_and_descendants`: [4](#0-3) [5](#0-4) [6](#0-5) 

`remove_expired` is the sole removal path that uses bare `remove_entry`. Notably, the function's own comment reads "Expire all transaction (and their dependencies) in the pool" — confirming the intended behavior is to remove descendants, which the implementation fails to do. [7](#0-6) 

`remove_entry_and_descendants` correctly handles this by first collecting all descendant IDs, removing all links, then removing each entry: [8](#0-7) 

## Impact Explanation

**False conflict detection blocking legitimate transactions.** `find_conflict_tx` consults `edges.inputs` to detect double-spends: [9](#0-8) 

Because `edges.inputs[O1] = tx_B` persists after `tx_A` expires, any new transaction attempting to spend `O1` is rejected as a conflict for up to 12 hours (until `tx_B` itself expires). An attacker can submit many parent-child pairs at low cost, wait for parents to expire, and systematically block legitimate transactions from spending specific outputs — fitting the allowed impact class **High: bad designs which could cause CKB network congestion with few costs**.

**Corrupted block templates.** If `tx_B` is in `Status::Proposed`, `package_txs` → `TxSelector` selects it for every block template. Blocks containing `tx_B` fail chain validation because `O1` does not exist. The miner's node rejects the block, requests a new template, and the cycle repeats until `tx_B` expires.

**Pool capacity waste.** Stale descendants count against `total_tx_size` and `total_tx_cycles`, reducing available capacity for legitimate transactions.

## Likelihood Explanation

Triggerable by any unprivileged user with no special access:
1. Submit `tx_A` (spends live cell `C1`, creates output `O1`) via standard RPC or P2P relay.
2. Submit `tx_B` (spends `O1`) one minute later.
3. Wait for `tx_A` to expire (default 12 hours).
4. `tx_B` remains in the pool for another ~12 hours with stale inputs.

Cost is two transaction fees. The attack is repeatable and scalable — an attacker can submit many such pairs to block many output points simultaneously. No majority hashpower, no privileged access, and no victim mistakes are required.

## Recommendation

Replace the bare `remove_entry` call in `remove_expired` with `remove_entry_and_descendants`, consistent with all other removal paths and with the function's own documented intent:

```rust
// tx-pool/src/pool.rs — remove_expired
for entry in removed {
    let tx_hash = entry.transaction().hash();
    debug!("remove_expired {} timestamp({})", tx_hash, entry.timestamp);
    let evicted = self.pool_map.remove_entry_and_descendants(&entry.proposal_short_id());
    for e in evicted {
        let reject = Reject::Expiry(e.timestamp);
        callbacks.call_reject(self, &e, reject);
    }
}
```

This mirrors `limit_size` and `remove_by_detached_proposal`, ensuring `entries`, `edges`, and `links` remain consistent when a transaction expires.

## Proof of Concept

1. Submit `tx_A` (spends live cell `C1`, creates output `O1`) to the tx-pool at time T.
2. Submit `tx_B` (spends `O1`) at time T+1 minute.
3. Both are proposed (appear in a block's proposal zone).
4. At time T+expiry_hours, `remove_expired` fires. Only `tx_A` is in the expired set.
5. `tx_A` is removed; `tx_B` remains in `Status::Proposed` with `edges.inputs[O1] = tx_B`.
6. `package_txs` includes `tx_B` in every subsequent block template.
7. Every mined block containing `tx_B` fails chain validation (`O1` does not exist).
8. Any attempt to submit `tx_C` spending `O1` is rejected by `find_conflict_tx` as a false double-spend.
9. This persists until `tx_B` expires at T+1min+expiry_hours.

A unit test can verify this directly: construct a `TxPool` with two chained transactions, advance the mock clock past the parent's expiry but before the child's expiry, call `remove_expired`, and assert that `pool_map.edges.inputs` contains no entry for the parent's output and that the child is absent from `pool_map.entries`.

### Citations

**File:** tx-pool/src/pool.rs (L270-270)
```rust
    // Expire all transaction (and their dependencies) in the pool.
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

**File:** tx-pool/src/pool.rs (L307-307)
```rust
                let removed = self.pool_map.remove_entry_and_descendants(&id);
```

**File:** tx-pool/src/pool.rs (L343-343)
```rust
                let mut entries = self.pool_map.remove_entry_and_descendants(id);
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

**File:** tx-pool/src/component/pool_map.rs (L294-298)
```rust
    pub(crate) fn find_conflict_tx(&self, tx: &TransactionView) -> HashSet<ProposalShortId> {
        tx.input_pts_iter()
            .filter_map(|out_point| self.edges.get_input_ref(&out_point).cloned())
            .collect()
    }
```

**File:** tx-pool/src/component/edges.rs (L8-15)
```rust
pub(crate) struct Edges {
    /// input-txid map represent in-pool tx's inputs
    pub(crate) inputs: HashMap<OutPoint, ProposalShortId>,
    /// dep-set<txid> map represent in-pool tx's deps
    pub(crate) deps: HashMap<OutPoint, HashSet<ProposalShortId>>,
    /// dep-set<txid-headers> map represent in-pool tx's header deps
    pub(crate) header_deps: HashMap<ProposalShortId, Vec<Byte32>>,
}
```

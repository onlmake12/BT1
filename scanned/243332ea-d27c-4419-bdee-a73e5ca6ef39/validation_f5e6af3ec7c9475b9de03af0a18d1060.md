### Title
Partial State Mutation Without Rollback in `PoolMap::add_entry` Leaves Link Graph Inconsistent With Entry Map — (File: `tx-pool/src/component/pool_map.rs`)

### Summary

`PoolMap::add_entry` performs a multi-step sequence of state mutations. `check_and_record_ancestors` writes parent/child relationships into `self.links` before `record_entry_edges` is called. If `record_entry_edges` returns `Err`, the function exits early without rolling back the link mutations, leaving `self.links` containing a dangling entry for a transaction that was never inserted into `self.entries`. The code itself acknowledges this rollback gap with a `FIXME` comment. Any subsequent traversal of the link graph that reaches the dangling short-id and calls `get_by_id_checked` will panic with `"inconsistent pool"`, crashing the node's tx-pool service.

### Finding Description

`add_entry` in `tx-pool/src/component/pool_map.rs` (lines 200–221) executes the following sequence:

```
1. updated_stat_for_add_tx(...)          // pure computation, no mutation
2. check_and_record_ancestors(...)       // MUTATES self.links (add_child, add_link)
3. record_entry_edges(...)               // MUTATES self.edges; can return Err
4. insert_entry(...)                     // MUTATES self.entries
5. record_entry_descendants(...)         // MUTATES self.links, self.entries
6. track_entry_statics(...)              // MUTATES counts
7. self.total_tx_size = total_tx_size    // MUTATES accounting
``` [1](#0-0) 

Step 2, `check_and_record_ancestors`, calls `_record_ancestors`, which unconditionally writes the new transaction's short-id into `self.links` via `self.links.add_child(parent, short_id)` and `self.links.add_link(short_id, TxLinks { parents, children: Default::default() })`: [2](#0-1) 

Step 3, `record_entry_edges`, iterates over the transaction's inputs and calls `self.edges.insert_input(i, tx_short_id)?`. `insert_input` returns `Err(Reject::RBFRejected(...))` if the input is already occupied (double-spend detected): [3](#0-2) [4](#0-3) 

When `record_entry_edges` returns `Err`, `add_entry` propagates the error immediately. Steps 4–7 are skipped. However, the mutations from step 2 are **not rolled back**: `self.links` now contains the new tx's short-id as a child of its parents, and the parents' `children` sets reference a short-id that has no corresponding entry in `self.entries`.

The code itself acknowledges this structural problem with a `FIXME` comment directly above `check_and_record_ancestors`: [5](#0-4) 

Additionally, `record_entry_edges` itself is not atomic: if a tx has N inputs and the k-th input fails, the first k-1 inputs were already inserted into `self.edges.inputs` and are also not rolled back.

The helper `get_by_id_checked`, called during link-graph traversals (e.g., `update_ancestors_index_key`, `update_descendants_index_key`), panics unconditionally if the short-id is not found in `self.entries`: [6](#0-5) 

### Impact Explanation

Once `self.links` contains a dangling short-id, any subsequent operation that traverses the ancestor or descendant graph of a parent transaction will encounter the dangling id and call `get_by_id_checked`, triggering a panic. In Rust, an unhandled `panic!` in a tokio task terminates that task. The tx-pool service runs as a long-lived async service; a panic in its core data structure handler crashes the pool, making the node unable to accept, relay, or mine transactions until restarted. This is a **permanent denial-of-service** against the local node's tx-pool, reachable by any peer or RPC caller who can submit transactions.

### Likelihood Explanation

The failure path requires `record_entry_edges` to return `Err` after `check_and_record_ancestors` has already written to `self.links`. In normal operation, the process layer resolves conflicts before calling `add_entry`, so `insert_input` should not encounter an occupied slot. However:

- The `FIXME` comment explicitly acknowledges the missing rollback, indicating the developers are aware the invariant can be violated.
- `record_entry_edges` is a defensive check, not a primary check; its failure path is reachable if the upstream conflict resolution is incomplete or if a race condition exists between conflict resolution and pool mutation.
- The partial mutation of `self.edges.inputs` within `record_entry_edges` itself (first k-1 inputs inserted, k-th fails) is an additional inconsistency that is never cleaned up.

Likelihood is **low** under normal conditions but **non-zero** given the acknowledged gap and the complexity of the RBF + ancestor-eviction interaction.

### Recommendation

1. **Restructure `add_entry` to be check-then-commit**: perform all validation and read-only checks first, then apply all mutations atomically only after all checks pass. This matches the comment's own suggestion: *"it's still safer to report an error before any writing kind of operation."*
2. **Add explicit rollback for `self.links`** if `record_entry_edges` fails: remove the link entry added by `_record_ancestors` and remove the new short-id from all parents' `children` sets.
3. **Make `record_entry_edges` atomic**: collect all inputs first, verify none are occupied, then insert all at once, or roll back already-inserted inputs on failure.
4. **Add an invariant assertion** (or a debug-mode consistency check) that verifies every short-id in `self.links` has a corresponding entry in `self.entries` after each `add_entry` call.

### Proof of Concept

1. Attacker submits transaction T1 spending input I to the pool. T1 is accepted; `self.edges.inputs[I] = T1.short_id`.
2. Attacker submits transaction T2 with two inputs: input J (free) and input I (already occupied by T1). T2 passes non-contextual checks and fee checks.
3. If the upstream conflict check does not evict T1 before calling `add_entry` (e.g., T2 is not recognized as an RBF replacement), `add_entry` is called with T2.
4. `check_and_record_ancestors` succeeds (T2 has no pool ancestors via J or I's tx-hash), calls `_record_ancestors`, and writes `T2.short_id` into `self.links`.
5. `record_entry_edges` inserts J into `self.edges.inputs` (succeeds), then tries to insert I (fails: already occupied by T1). Returns `Err(Reject::RBFRejected(...))`.
6. `add_entry` returns `Err`. `self.links` now has a dangling entry for T2. `self.edges.inputs[J] = T2.short_id` is

### Citations

**File:** tx-pool/src/component/pool_map.rs (L142-144)
```rust
    fn get_by_id_checked(&self, id: &ProposalShortId) -> &PoolEntry {
        self.get_by_id(id).expect("inconsistent pool")
    }
```

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

**File:** tx-pool/src/component/pool_map.rs (L462-485)
```rust
    fn record_entry_edges(&mut self, entry: &TxEntry) -> Result<(), Reject> {
        let tx_short_id: ProposalShortId = entry.proposal_short_id();
        let header_deps = entry.transaction().header_deps();
        let related_dep_out_points: Vec<_> = entry.related_dep_out_points().cloned().collect();
        let inputs = entry.transaction().input_pts_iter();

        // if input reference a in-pool output, connect it
        // otherwise, record input for conflict check
        for i in inputs {
            self.edges.insert_input(i.to_owned(), tx_short_id.clone())?;
        }

        // record dep-txid
        for d in related_dep_out_points {
            self.edges.insert_deps(d.to_owned(), tx_short_id.clone());
        }
        // record header_deps
        if !header_deps.is_empty() {
            self.edges
                .header_deps
                .insert(tx_short_id, header_deps.into_iter().collect());
        }
        Ok(())
    }
```

**File:** tx-pool/src/component/pool_map.rs (L556-580)
```rust
    fn _record_ancestors(
        &mut self,
        entry: &mut TxEntry,
        ancestors: HashSet<ProposalShortId>,
        parents: HashSet<ProposalShortId>,
    ) {
        // update parents references
        for ancestor_id in &ancestors {
            let ancestor = self.get_by_id_checked(ancestor_id);
            entry.add_ancestor_weight(&ancestor.inner);
        }

        let short_id = entry.proposal_short_id();

        for parent in &parents {
            self.links.add_child(parent, short_id.clone());
        }
        self.links.add_link(
            short_id,
            TxLinks {
                parents,
                children: Default::default(),
            },
        );
    }
```

**File:** tx-pool/src/component/pool_map.rs (L582-588)
```rust
    /// Check ancestors and record for entry
    // FIXME: In the scenario that a transaction passed all RBF rules, and then removed the conflicted
    // transaction in txpool, then failed with max ancestor limits, we now need to rollback the removing.
    // this is not an issue currently, because RBF have a rule that not allow any unknown inputs except
    // the conflicted inputs, so the new transaction can not be in a long transaction chain.
    // but it's still safer to report an error before any writing kind of operation.
    fn check_and_record_ancestors(
```

**File:** tx-pool/src/component/edges.rs (L33-54)
```rust
    pub(crate) fn insert_input(
        &mut self,
        out_point: OutPoint,
        txid: ProposalShortId,
    ) -> Result<(), Reject> {
        // inputs is occupied means double speanding happened here
        match self.inputs.entry(out_point.clone()) {
            Entry::Occupied(occupied) => {
                let msg = format!(
                    "txpool unexpected double-spending out_point: {:?} old_tx: {:?} new_tx: {:?}",
                    out_point,
                    occupied.get(),
                    txid
                );
                Err(Reject::RBFRejected(msg))
            }
            Entry::Vacant(vacant) => {
                vacant.insert(txid);
                Ok(())
            }
        }
    }
```

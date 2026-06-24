The code evidence is confirmed. Let me verify the `remove_entry_and_descendants` function and the eviction counter logic more carefully.

All claims are verified against the actual code. The vulnerability is real and well-evidenced.

Audit Report

## Title
Cell-dep Relationship Bypasses `max_ancestors_count` Limit, Enabling Pool Fan-out and Forced Eviction of Legitimate Transactions — (`tx-pool/src/component/pool_map.rs`)

## Summary

`check_and_record_ancestors` in `pool_map.rs` enforces `max_ancestors_count` only for input-based dependency chains. Transactions referencing an in-pool cell via `cell_dep` are each admitted with only 2 ancestors (the cell provider + self), allowing an attacker to accumulate N >> `max_ancestors_count` transactions in the pool. When the attacker later submits a transaction consuming that cell as an input, the eviction logic forcibly removes legitimate previously-accepted transactions. The integration test `TxPoolLimitAncestorCount` directly confirms both the bypass (2000 txs admitted when limit is 1000) and the forced eviction (1002 txs rejected).

## Finding Description

`get_tx_ancenstors` at [1](#0-0)  adds `cell_dep` parents to `parents` but never to `cell_ref_parents`. `cell_ref_parents` is only populated at [2](#0-1)  when processing inputs — it collects in-pool txs that have a `cell_dep` pointing to an out_point being consumed as an input by the new tx. So when tx B has a `cell_dep` to in-pool `tx_a`, `tx_a` enters `parents` but not `cell_ref_parents`, giving `ancestors_count = 2`.

The admission check at [3](#0-2)  passes unconditionally (`2 <= 1000`), so 2000 such transactions are admitted as confirmed by the test at [4](#0-3) .

When the attacker submits a transaction consuming `tx_a`'s output as an input, `cell_ref_parents` = 2000, `ancestors_count` = 2002. The condition at [5](#0-4)  evaluates `2002 - 2000 = 2 <= 1000`, entering the eviction path. The loop evicts 1002 transactions to bring `ancestors_count` to 1000, confirmed by the test at [6](#0-5) .

**Secondary counter bug:** The eviction loop at [7](#0-6)  calls `remove_entry_and_descendants` but decrements `ancestors_count` by only 1. `remove_entry_and_descendants` at [8](#0-7)  removes the entry AND all its descendants. Only the direct evicted entry is removed from `parents` via `parents.remove(next_id)`, leaving descendants of evicted entries as stale entries in `parents`.

After the loop, `calc_relation_ids` is called with stale `parents`. At [9](#0-8) , every id from `stage` is unconditionally inserted into `relation_ids` regardless of whether it exists in `self.inner`. Stale (already-removed) entries therefore inflate `ancestors.len()`, potentially causing the `assert!` at [10](#0-9)  to fire and crash the node process.

## Impact Explanation

**Primary — High: Vulnerabilities or bad designs which could cause CKB network congestion with few costs.** An attacker bypasses `max_ancestors_count` (default 1000, [11](#0-10) ) by submitting N >> 1000 cell_dep-referencing transactions, each admitted with `ancestors_count = 2`. This denies pool space to legitimate users. A single follow-up transaction then triggers mass eviction of previously-accepted legitimate transactions, forcing resubmission.

**Secondary — High: Vulnerabilities which could easily crash a CKB node.** If any evicted cell_ref_parent has in-pool descendants, the stale-entry inflation of `ancestors.len()` can cause the `assert!` at L636 to panic, crashing the node process.

## Likelihood Explanation

No special privileges are required. Any unprivileged `send_transaction` RPC caller or relay peer can execute the attack in three steps: (1) submit one root transaction creating a cell, (2) submit N transactions each referencing that cell as a `cell_dep` (each passes admission with `ancestors_count = 2`), (3) submit one transaction consuming that cell as an input, triggering eviction of `N - max_ancestors_count + 2` previously-accepted transactions. At the default minimum fee rate of 1000 shannons/kB and 180 MB pool size, thousands of small transactions can be submitted cheaply. The attack is repeatable with no victim mistakes required.

## Recommendation

1. **Count cell_dep ancestors toward the per-transaction admission limit**: In `get_tx_ancenstors`, track cell_dep-based parents separately and include their count in the admission check at L598, not only in the eviction bypass at L603.

2. **Bound the number of in-pool transactions that can reference any single out_point as a cell_dep**: Add a `max_cell_dep_ref_count` limit in `Edges::insert_deps` that rejects new transactions if too many in-pool transactions already reference the same out_point as a cell_dep.

3. **Fix the eviction loop counter**: After each `remove_entry_and_descendants` call, decrement `ancestors_count` by `removed.len()` (the actual number of removed entries), not by a fixed 1. Also remove all descendants of the evicted entry from `parents` to prevent stale entries from inflating the recomputed `ancestors`.

4. **Replace the `assert!` with a graceful error**: Replace `assert!(ancestors.len() < self.max_ancestors_count)` at L636 with `return Err(Reject::ExceededMaximumAncestorsCount)` to prevent a node crash.

## Proof of Concept

The integration test `TxPoolLimitAncestorCount` in `test/src/specs/tx_pool/limit.rs` directly demonstrates the bypass:

1. Submit `tx_a` (root transaction creating a cell).
2. Submit 2000 transactions each with `cell_dep` pointing to `tx_a`'s output — all succeed (`res.is_ok()` asserted), far exceeding `max_ancestors_count = 1000`. [4](#0-3) 
3. Submit a transaction consuming `tx_a`'s output as an input — succeeds, triggering eviction of 1002 previously-accepted transactions, all marked `Rejected`. [6](#0-5) 

To reproduce against a live node: submit one root transaction via `send_transaction` RPC, then submit 2000 transactions each referencing its output as a `cell_dep`, then submit a transaction spending that output as an input. Observe that 1002 previously-accepted transactions are marked `Rejected`.

### Citations

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

**File:** tx-pool/src/component/pool_map.rs (L531-534)
```rust
            if let Some(deps) = self.edges.deps.get(&input_pt) {
                cell_ref_parents.extend(deps.iter().cloned());
                parents.extend(deps.iter().cloned());
            }
```

**File:** tx-pool/src/component/pool_map.rs (L541-547)
```rust
        for cell_dep in entry.cell_deps() {
            let dep_pt = cell_dep.out_point();
            let id = ProposalShortId::from_tx_hash(&dep_pt.tx_hash());
            if self.links.inner.contains_key(&id) {
                parents.insert(id);
            }
        }
```

**File:** tx-pool/src/component/pool_map.rs (L598-601)
```rust
        if ancestors_count <= self.max_ancestors_count {
            self._record_ancestors(entry, ancestors, parents);
            return Ok(evicted);
        }
```

**File:** tx-pool/src/component/pool_map.rs (L603-603)
```rust
        if ancestors_count.saturating_sub(cell_ref_parents.len()) <= self.max_ancestors_count {
```

**File:** tx-pool/src/component/pool_map.rs (L618-621)
```rust
                    let removed = self.remove_entry_and_descendants(next_id);
                    ancestors_count = ancestors_count.saturating_sub(1);
                    parents.remove(next_id);
                    evicted.extend(removed);
```

**File:** tx-pool/src/component/pool_map.rs (L636-636)
```rust
        assert!(ancestors.len() < self.max_ancestors_count);
```

**File:** test/src/specs/tx_pool/limit.rs (L93-101)
```rust
        for i in 1..=2000 {
            let cur = always_success_transaction(node0, initial_inputs.get(i).unwrap());
            let cur = cur.as_advanced_builder().cell_dep(cell_dep.clone()).build();
            let res = node0
                .rpc_client()
                .send_transaction_result(cur.data().into());
            assert!(res.is_ok());
            cell_ref_txs.push(cur.clone());
        }
```

**File:** test/src/specs/tx_pool/limit.rs (L113-126)
```rust
        let res = node0
            .rpc_client()
            .send_transaction_result(last.data().into());
        assert!(res.is_ok());

        // assert the first 127 in 250 transactions are evicated.
        for (i, tx) in cell_ref_txs.iter().enumerate() {
            let res = node0
                .rpc_client()
                .get_transaction_with_verbosity(tx.hash(), 2);
            if i < 1002 {
                assert!(matches!(res.tx_status.status, Status::Rejected));
            }
        }
```

**File:** tx-pool/src/component/links.rs (L68-69)
```rust
            stage.remove(&id);
            relation_ids.insert(id);
```

**File:** util/app-config/src/legacy/tx_pool.rs (L16-16)
```rust
const DEFAULT_MAX_ANCESTORS_COUNT: usize = 1_000;
```

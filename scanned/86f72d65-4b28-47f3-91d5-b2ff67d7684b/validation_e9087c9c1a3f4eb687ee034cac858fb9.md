Audit Report

## Title
Cell-dep Relationship Bypasses `max_ancestors_count` Limit, Enabling Pool Fan-out and Forced Eviction of Legitimate Transactions — (`tx-pool/src/component/pool_map.rs`)

## Summary

`check_and_record_ancestors` in `pool_map.rs` enforces `max_ancestors_count` only for input-based dependency chains. Transactions referencing an in-pool cell via `cell_dep` are each admitted with only 1 ancestor, allowing an attacker to accumulate N >> `max_ancestors_count` transactions in the pool that all depend on a single in-pool transaction. When the attacker later submits a transaction consuming that cell as an input, the eviction logic forcibly removes legitimate transactions from the pool. The integration test `TxPoolLimitAncestorCount` directly confirms this behavior.

## Finding Description

`get_tx_ancenstors` computes three sets from the new transaction's inputs and cell_deps:

- `parents`: all direct in-pool parents (from both inputs AND cell_deps)
- `cell_ref_parents`: in-pool txs that have a `cell_dep` pointing to an out_point the new tx is consuming as an input
- `ancestors`: full transitive closure of `parents` [1](#0-0) 

When a transaction is submitted with a `cell_dep` pointing to an in-pool `tx_a`, `tx_a` is added to `parents`, and `ancestors` = `{tx_a}`, giving `ancestors_count = 2`. Since `2 <= max_ancestors_count (1000)`, the transaction is admitted unconditionally: [2](#0-1) 

This means N >> `max_ancestors_count` transactions can all be admitted, each referencing the same in-pool cell via `cell_dep`. The integration test confirms 2000 such transactions are admitted when the limit is 1000: [3](#0-2) 

When the attacker submits a transaction consuming `tx_a`'s output as an input, `get_tx_ancenstors` now returns `ancestors_count = 2002` (2000 cell_ref_parents + tx_a + self). The condition at L603 checks whether the excess is attributable to `cell_ref_parents`: [4](#0-3) 

`2002 - 2000 = 2 <= 1000`, so the eviction path is entered. The loop evicts 1002 transactions (the lowest-fee cell_ref_parents) to bring `ancestors_count` to 1000. The test confirms 1002 transactions are forcibly evicted and marked `Rejected`: [5](#0-4) 

**Secondary counter bug:** The eviction loop decrements `ancestors_count` by 1 per call to `remove_entry_and_descendants`, but that function removes the entry AND all its descendants: [6](#0-5) 

If a cell_ref_parent has descendants that are also cell_ref_parents, those descendants are removed from the pool in the first call but remain in `parents`. After the loop, `calc_relation_ids` is called with stale `parents` entries. Because `calc_relation_ids` includes any ID in the initial stage regardless of whether it is still in `links.inner`: [7](#0-6) 

stale (already-removed) entries can appear in the recomputed `ancestors`, inflating `ancestors.len()`. In the worst case this causes the `assert!` at L636 to fire, crashing the node process: [8](#0-7) 

## Impact Explanation

**High — Vulnerabilities or bad designs which could cause CKB network congestion with few costs.**

An attacker can fill the mempool with N >> `max_ancestors_count` transactions (e.g., 2000 when the limit is 1000) at minimum fee rate, bypassing the protection `max_ancestors_count` was designed to provide. This denies pool space to legitimate users. The attacker can then trigger mass eviction of legitimate transactions by submitting a single consuming transaction, forcing those users to resubmit. The pool-filling and forced eviction are both confirmed by the integration test and are reachable at low cost.

The secondary counter bug can additionally cause the `assert!` at L636 to panic, crashing the node process (High — node crash). [9](#0-8) 

## Likelihood Explanation

The attack requires no special privileges. Any unprivileged `send_transaction` RPC caller or relay peer can execute it in three steps:

1. Submit one transaction creating a cell (normal fee).
2. Submit N transactions using that cell as a `cell_dep` (each individually passes all admission checks with `ancestors_count = 2`).
3. Submit one transaction consuming that cell as an input (triggers eviction of N - `max_ancestors_count` + 2 transactions).

At minimum fee rate (1000 shannons/kB) and the default 180 MB pool size, thousands of small transactions can be submitted cheaply. The attack is repeatable and requires no victim mistakes.

## Recommendation

1. **Count cell_dep ancestors toward the per-transaction admission limit**: When computing `ancestors_count` for admission, count all in-pool transactions reachable via both inputs and cell_deps. Do not grant a special bypass for cell_ref_parents during admission — only allow the eviction path when the new transaction is a legitimate consumer of the cell.

2. **Bound the number of in-pool transactions that can reference any single out_point as a cell_dep**: Add a `max_cell_dep_ref_count` limit in `Edges::insert_deps` that rejects new transactions if too many in-pool transactions already reference the same out_point as a cell_dep.

3. **Fix the eviction loop counter**: After each `remove_entry_and_descendants` call, decrement `ancestors_count` by the actual number of removed entries (i.e., `removed.len()`), not by a fixed 1. Also remove all descendants of the evicted entry from `parents` to prevent stale entries from inflating the recomputed `ancestors`.

4. **Replace the `assert!` with a graceful error**: The `assert!(ancestors.len() < self.max_ancestors_count)` at L636 can crash the node process. Replace it with `return Err(Reject::ExceededMaximumAncestorsCount)`.

## Proof of Concept

The integration test `TxPoolLimitAncestorCount` in `test/src/specs/tx_pool/limit.rs` directly and completely demonstrates the bypass: [10](#0-9) 

- 2000 cell_dep transactions are submitted successfully (each with `ancestors_count = 2`), far exceeding `max_ancestors_count = 1000`.
- A transaction consuming `tx_a`'s output is submitted, triggering eviction of 1002 transactions.
- `res.is_ok()` is asserted for the consuming transaction, confirming the bypass and forced eviction are the current behavior.

To reproduce against a live node: submit one root transaction, then submit 2000 transactions each referencing its output as a `cell_dep` via the `send_transaction` RPC, then submit a transaction spending that output as an input. Observe that 1002 previously-accepted transactions are marked `Rejected`.

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

**File:** tx-pool/src/component/pool_map.rs (L515-554)
```rust
    // return (ancestors, parents, cell_ref_parents)
    // `cell_ref_parents` may be invalidate when the tx consuming the cell is submitted
    fn get_tx_ancenstors(
        &self,
        entry: &TransactionView,
    ) -> (
        HashSet<ProposalShortId>,
        HashSet<ProposalShortId>,
        HashSet<ProposalShortId>,
    ) {
        let mut parents: HashSet<ProposalShortId> =
            HashSet::with_capacity(entry.inputs().len() + entry.cell_deps().len());
        let mut cell_ref_parents: HashSet<ProposalShortId> = Default::default();

        for input in entry.inputs() {
            let input_pt = input.previous_output();
            if let Some(deps) = self.edges.deps.get(&input_pt) {
                cell_ref_parents.extend(deps.iter().cloned());
                parents.extend(deps.iter().cloned());
            }

            let id = ProposalShortId::from_tx_hash(&input_pt.tx_hash());
            if self.links.inner.contains_key(&id) {
                parents.insert(id);
            }
        }
        for cell_dep in entry.cell_deps() {
            let dep_pt = cell_dep.out_point();
            let id = ProposalShortId::from_tx_hash(&dep_pt.tx_hash());
            if self.links.inner.contains_key(&id) {
                parents.insert(id);
            }
        }

        let ancestors = self
            .links
            .calc_relation_ids(parents.clone(), Relation::Parents);

        (ancestors, parents, cell_ref_parents)
    }
```

**File:** tx-pool/src/component/pool_map.rs (L595-601)
```rust
        let mut ancestors_count = ancestors.len() + 1;
        let mut evicted = Default::default();

        if ancestors_count <= self.max_ancestors_count {
            self._record_ancestors(entry, ancestors, parents);
            return Ok(evicted);
        }
```

**File:** tx-pool/src/component/pool_map.rs (L603-628)
```rust
        if ancestors_count.saturating_sub(cell_ref_parents.len()) <= self.max_ancestors_count {
            // if ancestors count exceed limitation,
            // try to evict some conflicted transactions due to ref cells

            // sort them to find out the transactions with lowest fees
            let evict_candidates: Vec<ProposalShortId> = self
                .entries
                .iter_by_evict_key()
                .filter(move |entry| cell_ref_parents.contains(&entry.id))
                .map(|x| x.id.clone())
                .collect();

            let mut iter = evict_candidates.iter();
            while ancestors_count > self.max_ancestors_count {
                if let Some(next_id) = iter.next() {
                    let removed = self.remove_entry_and_descendants(next_id);
                    ancestors_count = ancestors_count.saturating_sub(1);
                    parents.remove(next_id);
                    evicted.extend(removed);
                } else {
                    break;
                }
            }
        } else {
            return Err(Reject::ExceededMaximumAncestorsCount);
        }
```

**File:** tx-pool/src/component/pool_map.rs (L630-639)
```rust
        // some txs in `parents` are removed, now `ancestors` need to re-caculate,
        let ancestors = self
            .links
            .calc_relation_ids(parents.clone(), Relation::Parents);

        // we can assume the number now is less than `max_ancestors_count`
        assert!(ancestors.len() < self.max_ancestors_count);

        self._record_ancestors(entry, ancestors, parents);
        Ok(evicted)
```

**File:** test/src/specs/tx_pool/limit.rs (L70-127)
```rust
pub struct TxPoolLimitAncestorCount;
impl Spec for TxPoolLimitAncestorCount {
    fn run(&self, nodes: &mut Vec<Node>) {
        let node0 = &nodes[0];

        let initial_inputs = gen_spendable(node0, 3100);
        let input_a = &initial_inputs[0];

        // Commit transaction root
        let tx_a = {
            let tx_a = always_success_transaction(node0, input_a);
            node0.submit_transaction(&tx_a);
            tx_a
        };

        let cell_dep = CellDepBuilder::default()
            .dep_type(DepType::Code)
            .out_point(OutPoint::new(tx_a.hash(), 0))
            .build();

        // Create 250 transactions cell dep on tx_a
        // we can have more than config.max_ancestors_count number of txs using one cell ref
        let mut cell_ref_txs = vec![];
        for i in 1..=2000 {
            let cur = always_success_transaction(node0, initial_inputs.get(i).unwrap());
            let cur = cur.as_advanced_builder().cell_dep(cell_dep.clone()).build();
            let res = node0
                .rpc_client()
                .send_transaction_result(cur.data().into());
            assert!(res.is_ok());
            cell_ref_txs.push(cur.clone());
        }

        // Create a new transaction consume the cell dep, it will be succeed in submit
        let input = CellMetaBuilder::from_cell_output(tx_a.output(0).unwrap(), Default::default())
            .out_point(OutPoint::new(tx_a.hash(), 0))
            .build();
        let last = always_success_transaction(node0, &input);

        // now there are 2002 ancestors for the last tx in the pool:
        // 2002 = 2000 ref cell + 1 parent + 1 for self
        // to make sure this consuming cell dep transaction submitted,
        // we need to evict 1002 = 2002 - 1000 cell ref transactions
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

**File:** tx-pool/src/component/links.rs (L52-72)
```rust
    pub fn calc_relation_ids(
        &self,
        mut stage: HashSet<ProposalShortId>,
        relation: Relation,
    ) -> HashSet<ProposalShortId> {
        let mut relation_ids = HashSet::with_capacity(stage.len());

        while let Some(id) = stage.iter().next().cloned() {
            //recursively
            if let Some(tx_links) = self.inner.get(&id) {
                for direct_id in tx_links.get_direct_ids(relation) {
                    if !relation_ids.contains(direct_id) {
                        stage.insert(direct_id.clone());
                    }
                }
            }
            stage.remove(&id);
            relation_ids.insert(id);
        }
        relation_ids
    }
```

**File:** util/app-config/src/legacy/tx_pool.rs (L16-16)
```rust
const DEFAULT_MAX_ANCESTORS_COUNT: usize = 1_000;
```

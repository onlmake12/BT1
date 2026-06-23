### Title
Cell-dep Relationship Bypasses `max_ancestors_count` Limit, Enabling Unbounded Pool Fan-out and Forced Eviction of Legitimate Transactions — (`tx-pool/src/component/pool_map.rs`)

---

### Summary

The `check_and_record_ancestors` function in `pool_map.rs` enforces `max_ancestors_count` only for input-based (spending) dependency chains. Transactions that reference an in-pool cell via `cell_dep` are admitted to the pool without being individually counted against the ancestor limit. This allows an attacker to accumulate an unbounded number of transactions in the pool that all depend on a single in-pool transaction — far exceeding `max_ancestors_count` — bypassing the protection the limit was designed to provide. When the attacker later submits a transaction consuming that cell as an input, the eviction logic in `check_and_record_ancestors` forcibly removes legitimate transactions from the pool.

---

### Finding Description

The `max_ancestors_count` limit was introduced to bound the number of in-pool transactions that can form a dependency chain on any single transaction, preventing expensive ancestor-graph traversals and pool-filling attacks. However, the admission check in `check_and_record_ancestors` only rejects a transaction when its ancestor count exceeds the limit **and** the excess cannot be attributed to `cell_ref_parents` (transactions that use the same out_point as a cell_dep that the new tx is consuming as an input).

The `get_tx_ancenstors` function computes three sets: [1](#0-0) 

- `parents`: all direct parents (from both inputs AND cell_deps)
- `cell_ref_parents`: only parents that are "dep-referencing" transactions (in-pool txs that have a cell_dep pointing to an out_point the new tx is consuming as input)
- `ancestors`: full transitive closure of `parents`

When a new transaction is submitted, each cell_dep transaction individually has only 1 ancestor (the transaction creating the cell), so each passes the `max_ancestors_count` check independently: [2](#0-1) 

This means N >> `max_ancestors_count` transactions can all be admitted to the pool, each referencing the same in-pool cell via `cell_dep`. The integration test explicitly documents this behavior: [3](#0-2) 

When the attacker later submits a transaction consuming that cell as an input, `check_and_record_ancestors` detects that `ancestors_count` exceeds the limit but the excess is due to `cell_ref_parents`, and enters the eviction path: [4](#0-3) 

This evicts `ancestors_count - max_ancestors_count` transactions from the pool. With 2000 cell_dep transactions and `max_ancestors_count = 1000`, 1001 transactions are forcibly evicted.

The eviction counter is decremented by 1 per removed entry, but `remove_entry_and_descendants` removes the entry **and all its descendants**: [5](#0-4) 

This means the actual number of evicted transactions can be much larger than `ancestors_count - max_ancestors_count` if any cell_ref_parent has descendants of its own, while the loop counter only decrements by 1 per iteration. The loop may terminate early (via `break`) leaving `ancestors_count` still above `max_ancestors_count`, yet the code proceeds to insert the new entry anyway: [6](#0-5) 

After the loop, the assertion `assert!(ancestors.len() < self.max_ancestors_count)` at line 636 may panic if the eviction loop exhausted all candidates before bringing the count below the limit, causing a node crash.

---

### Impact Explanation

1. **Pool-filling bypass**: An attacker can submit N >> `max_ancestors_count` transactions (e.g., 2000 when the limit is 1000) that all depend on a single in-pool transaction via `cell_dep`. Each is individually admitted because each has only 1 ancestor. This fills the pool with attacker-controlled transactions, denying pool space to legitimate users — analogous to how `shareKey` exhausted the key supply without purchasing.

2. **Forced eviction of legitimate transactions**: If legitimate users submit transactions that use an in-pool cell as a `cell_dep`, the attacker (who controls the transaction creating that cell) can submit a transaction consuming that cell as an input, triggering mass eviction of the legitimate users' transactions. The evicted transactions are still structurally valid but are removed from the pool, requiring resubmission.

3. **Potential assertion panic / node crash**: If the eviction loop exhausts all `cell_ref_parents` without bringing `ancestors_count` below `max_ancestors_count`, the `assert!` at line 636 fires, crashing the node process.

---

### Likelihood Explanation

The attack is reachable by any unprivileged `send_transaction` RPC caller or transaction relay peer. No special privileges are required. The attacker only needs to:

1. Submit one transaction creating a cell (paying normal fees)
2. Submit N transactions using that cell as a `cell_dep` (each paying normal fees, each individually passing all admission checks)
3. Submit one transaction consuming that cell as an input

The default `max_ancestors_count` is 1000 (from `util/app-config/src/legacy/tx_pool.rs`): [7](#0-6) 

The pool size limit (`max_tx_pool_size`, default 180 MB) bounds the total number of transactions, but at minimum fee rate, an attacker can fill the pool with thousands of small cell_dep transactions cheaply.

---

### Recommendation

1. **Count cell_dep ancestors toward the per-transaction admission limit**: When a transaction is admitted to the pool, count all in-pool transactions that it depends on (via both inputs and cell_deps) toward `max_ancestors_count`. This prevents the fan-out bypass.

2. **Bound the number of in-pool transactions that can reference any single out_point as a cell_dep**: Add a limit (e.g., `max_cell_dep_ref_count`) to `Edges::insert_deps` that rejects new transactions if too many in-pool transactions already reference the same out_point as a cell_dep.

3. **Fix the eviction loop counter**: The loop decrements `ancestors_count` by 1 per `remove_entry_and_descendants` call, but that call may remove multiple entries. The counter should be decremented by the actual number of removed entries to avoid the assertion panic.

4. **Remove the `assert!` or replace with a graceful error**: The `assert!(ancestors.len() < self.max_ancestors_count)` at line 636 can panic the node process. Replace with a `Reject::ExceededMaximumAncestorsCount` return.

---

### Proof of Concept

The integration test `TxPoolLimitAncestorCount` in `test/src/specs/tx_pool/limit.rs` directly demonstrates the bypass: [8](#0-7) 

- 2000 cell_dep transactions are submitted successfully (each with 1 ancestor = `tx_a`), far exceeding `max_ancestors_count = 1000`.
- A transaction consuming `tx_a`'s output is then submitted, triggering eviction of 1002 transactions.
- The test asserts `res.is_ok()` for the consuming transaction, confirming the bypass and forced eviction are the intended (but vulnerable) behavior.

An attacker can replicate this against a live node via the `send_transaction` RPC endpoint, filling the pool and/or evicting targeted transactions.

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

**File:** tx-pool/src/component/pool_map.rs (L603-639)
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

**File:** util/app-config/src/legacy/tx_pool.rs (L16-16)
```rust
const DEFAULT_MAX_ANCESTORS_COUNT: usize = 1_000;
```

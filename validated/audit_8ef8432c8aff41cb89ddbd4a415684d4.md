### Title
Cell-Dep Consumption Evicts Arbitrary Pool Transactions, Enabling Persistent Tx-Pool DoS — (`tx-pool/src/component/pool_map.rs`)

### Summary

An unprivileged transaction sender can evict an unbounded number of legitimate pending transactions from the CKB tx-pool by submitting a transaction that spends a cell that other pool transactions reference as a `cell_dep`. The `resolve_conflict` path unconditionally removes every in-pool transaction whose `cell_dep` points to the consumed output, and the `check_and_record_ancestors` path additionally evicts the lowest-fee subset of those same transactions when the ancestor-count limit would be exceeded. Because the attacker controls when the cell is spent, the attack is repeatable: a new "trap" cell can be created and advertised after each round, making the DoS persistent.

---

### Finding Description

**Root cause — `resolve_conflict` (pool_map.rs lines 305–332)**

When a new transaction is submitted, `resolve_conflict` iterates over every input of the incoming transaction. For each input out-point it calls `self.edges.remove_deps(&i)`, which returns the set of all in-pool transactions that declared that out-point as a `cell_dep`. Every such transaction — and all of its descendants — is immediately removed from the pool with `Reject::Resolve(OutPointError::Dead(...))`. [1](#0-0) 

There is no cap on how many transactions can be evicted in a single call; the only bound is the total number of pool entries that share the dep.

**Amplifier — `check_and_record_ancestors` (pool_map.rs lines 588–640)**

Even before `resolve_conflict` runs, `get_tx_ancenstors` counts every in-pool transaction that uses the same out-point as a `cell_dep` as a "cell_ref_parent" ancestor of the incoming transaction. [2](#0-1) 

If the resulting ancestor count exceeds `max_ancestors_count` (default 1 000), `check_and_record_ancestors` evicts the lowest-fee cell_ref_parents one by one until the count falls within the limit, and the incoming transaction is accepted. [3](#0-2) 

The integration test `TxPoolLimitAncestorCount` explicitly demonstrates this: 2 000 transactions reference `tx_a` as a `cell_dep`; submitting a transaction that spends `tx_a` causes 1 002 of them to be evicted and the spending transaction to be accepted. [4](#0-3) 

**Shared-state analogy to the original report**

The `edges.deps` map is the shared mutable state: [5](#0-4) 

Just as RocketPool's deposit-delay timer is reset by any user's deposit and blocks all withdrawals, the `edges.deps` map is modified by any transaction submission that spends a shared cell, and that single submission evicts all transactions that depend on it.

---

### Impact Explanation

- **Tx-pool DoS**: An attacker who owns a cell that many users reference as a `cell_dep` (e.g., a script cell they published and advertised) can evict all those users' pending transactions in a single RPC call to `send_transaction`.
- **Fund delay / loss**: Evicted transactions must be resubmitted. If the evicted transactions are time-sensitive (e.g., they carry a `since` lock that is about to expire, or they are part of a multi-step protocol), the user may miss the window and lose funds or protocol state.
- **Repeatability**: After the attacker's spending transaction is committed, they can create a new cell, wait for users to adopt it as a `cell_dep`, and repeat the attack indefinitely.

---

### Likelihood Explanation

- The attacker needs to own a cell that other users reference as a `cell_dep`. This is realistic: any script author who publishes a new lock or type script controls such a cell.
- The attack requires only a single `send_transaction` RPC call — no special privilege, no majority hash-power, no Sybil attack.
- The `resolve_conflict` eviction path is unconditional and has no rate limit. [6](#0-5) 

---

### Recommendation

1. **Decouple cell-dep eviction from input consumption.** When a cell is spent, transactions that reference it as a `cell_dep` should be moved to an "orphan" or "suspended" state rather than immediately evicted. They can be re-evaluated once the spending transaction is committed or dropped.
2. **Cap the number of transactions evicted per submission.** Introduce a per-submission eviction limit so that a single `send_transaction` call cannot remove an unbounded number of pool entries.
3. **Charge for eviction.** Require the incoming transaction to pay a fee proportional to the number of pool entries it displaces, similar to RBF's `min_replace_fee` rule. [7](#0-6) 

---

### Proof of Concept

```
1. Attacker publishes cell X (a script cell) via send_transaction.
   Cell X is mined into a block.

2. Victims V1…V2000 each submit a transaction that:
   - spends their own inputs
   - declares cell X as a cell_dep
   All 2000 transactions are accepted into the pool.

3. Attacker submits transaction T_attack that spends cell X as an input.

4. Inside submit_entry → resolve_conflict:
   edges.remove_deps(cell_X_outpoint) returns {V1…V2000}.
   All 2000 victim transactions are removed with Reject::Resolve(Dead).

   Alternatively, inside check_and_record_ancestors:
   ancestors_count = 2001 > max_ancestors_count (1000).
   The 1001 lowest-fee victim transactions are evicted so T_attack is accepted.

5. Victims must resubmit. Attacker repeats with a new cell.
``` [8](#0-7) [9](#0-8)

### Citations

**File:** tx-pool/src/component/pool_map.rs (L305-332)
```rust
    pub(crate) fn resolve_conflict(&mut self, tx: &TransactionView) -> Vec<ConflictEntry> {
        let mut conflicts = Vec::new();

        for i in tx.input_pts_iter() {
            if let Some(id) = self.edges.remove_input(&i) {
                let entries = self.remove_entry_and_descendants(&id);
                if !entries.is_empty() {
                    let reject = Reject::Resolve(OutPointError::Dead(i.clone()));
                    let rejects = std::iter::repeat_n(reject, entries.len());
                    conflicts.extend(entries.into_iter().zip(rejects));
                }
            }

            // deps consumed
            if let Some(x) = self.edges.remove_deps(&i) {
                for id in x {
                    let entries = self.remove_entry_and_descendants(&id);
                    if !entries.is_empty() {
                        let reject = Reject::Resolve(OutPointError::Dead(i.clone()));
                        let rejects = std::iter::repeat_n(reject, entries.len());
                        conflicts.extend(entries.into_iter().zip(rejects));
                    }
                }
            }
        }

        conflicts
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

**File:** tx-pool/src/component/pool_map.rs (L588-640)
```rust
    fn check_and_record_ancestors(
        &mut self,
        entry: &mut TxEntry,
    ) -> Result<HashSet<TxEntry>, Reject> {
        let tx = entry.transaction();
        let (ancestors, mut parents, cell_ref_parents) = self.get_tx_ancenstors(tx);

        let mut ancestors_count = ancestors.len() + 1;
        let mut evicted = Default::default();

        if ancestors_count <= self.max_ancestors_count {
            self._record_ancestors(entry, ancestors, parents);
            return Ok(evicted);
        }

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
    }
```

**File:** test/src/specs/tx_pool/limit.rs (L103-126)
```rust
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

**File:** tx-pool/src/pool.rs (L574-679)
```rust
    pub(crate) fn check_rbf(
        &self,
        snapshot: &Snapshot,
        entry: &TxEntry,
    ) -> Result<HashSet<ProposalShortId>, Reject> {
        assert!(self.enable_rbf());
        let tx_inputs: Vec<OutPoint> = entry.transaction().input_pts_iter().collect();
        let conflict_ids = self.pool_map.find_conflict_tx(entry.transaction());

        if conflict_ids.is_empty() {
            return Ok(HashSet::new());
        }

        let short_id = entry.proposal_short_id();

        // Rule #1, the node has enabled RBF, which is checked by caller
        let conflicts = conflict_ids
            .iter()
            .filter_map(|id| self.get_pool_entry(id))
            .collect::<Vec<_>>();
        assert!(conflicts.len() == conflict_ids.len());

        // Rule #2, new tx don't contain any new unconfirmed inputs
        let mut inputs = HashSet::new();
        for c in conflicts.iter() {
            inputs.extend(c.inner.transaction().input_pts_iter());
        }

        if tx_inputs
            .iter()
            .any(|pt| !inputs.contains(pt) && !snapshot.transaction_exists(&pt.tx_hash()))
        {
            return Err(Reject::RBFRejected(
                "new Tx contains unconfirmed inputs".to_string(),
            ));
        }

        // Rule #5, the replaced tx's descendants can not more than 100
        // and the ancestor of the new tx don't have common set with the replaced tx's descendants
        let mut replace_count: usize = 0;
        let mut all_conflicted = conflicts.clone();
        let ancestors = self.pool_map.calc_ancestors(&short_id);
        for conflict in conflicts.iter() {
            let descendants = self.pool_map.calc_descendants(&conflict.id);
            replace_count += descendants.len() + 1;
            if replace_count > MAX_REPLACEMENT_CANDIDATES {
                return Err(Reject::RBFRejected(format!(
                    "Tx conflict with too many txs, conflict txs count: {}, expect <= {}",
                    replace_count, MAX_REPLACEMENT_CANDIDATES,
                )));
            }

            if !descendants.is_disjoint(&ancestors) {
                return Err(Reject::RBFRejected(
                    "Tx ancestors have common with conflict Tx descendants".to_string(),
                ));
            }

            let entries = descendants
                .iter()
                .filter_map(|id| self.get_pool_entry(id))
                .collect::<Vec<_>>();

            for entry in entries.iter() {
                let hash = entry.inner.transaction().hash();
                if tx_inputs.iter().any(|pt| pt.tx_hash() == hash) {
                    return Err(Reject::RBFRejected(
                        "new Tx contains inputs in descendants of to be replaced Tx".to_string(),
                    ));
                }
            }
            all_conflicted.extend(entries);
        }

        let tx_cells_deps: Vec<OutPoint> = entry
            .transaction()
            .cell_deps_iter()
            .map(|c| c.out_point())
            .collect();
        for entry in all_conflicted.iter() {
            let hash = entry.inner.transaction().hash();
            if tx_cells_deps.iter().any(|pt| pt.tx_hash() == hash) {
                return Err(Reject::RBFRejected(
                    "new Tx contains cell deps from conflicts".to_string(),
                ));
            }
        }

        // Rule #4, new tx's fee need to higher than min_rbf_fee computed from the tx_pool configuration
        // Rule #3, new tx's fee need to higher than conflicts, here we only check the all conflicted txs fee
        let fee = entry.fee;
        if let Some(min_replace_fee) = self.calculate_min_replace_fee(&all_conflicted, entry.size) {
            if fee < min_replace_fee {
                return Err(Reject::RBFRejected(format!(
                    "Tx's current fee is {}, expect it to >= {} to replace old txs",
                    fee, min_replace_fee,
                )));
            }
        } else {
            return Err(Reject::RBFRejected(
                "calculate_min_replace_fee failed".to_string(),
            ));
        }

        Ok(conflict_ids)
    }
```

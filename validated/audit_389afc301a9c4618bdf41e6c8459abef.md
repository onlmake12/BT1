Audit Report

## Title
Cell-dep Relationship Bypasses `max_ancestors_count` Limit, Enabling Pool-Filling and Forced Eviction of Legitimate Transactions — (`tx-pool/src/component/pool_map.rs`)

## Summary

In `get_tx_ancenstors`, transactions referencing an in-pool cell via `cell_dep` are admitted with only 1 ancestor each, bypassing the `max_ancestors_count` limit. An attacker can accumulate far more than `max_ancestors_count` such transactions in the pool. When the attacker later submits a transaction consuming that cell as an input, the eviction logic forcibly removes legitimate in-pool transactions. The integration test `TxPoolLimitAncestorCount` explicitly confirms both the pool-filling and forced eviction behaviors.

## Finding Description

In `get_tx_ancenstors` ( [1](#0-0) ), `cell_ref_parents` is populated only when a new transaction's **input** `out_point` matches an existing in-pool dep reference ( [2](#0-1) ). When a transaction uses an in-pool cell purely as a `cell_dep`, it is added to `parents` but not to `cell_ref_parents` ( [3](#0-2) ). Its ancestor set is just the single in-pool transaction that created the cell, so `ancestors_count = 2`, well below `max_ancestors_count = 1000`.

The admission check at [4](#0-3)  passes for each such transaction individually. The test confirms 2000 such transactions are all accepted: [5](#0-4) 

When the attacker submits a transaction consuming that cell as an **input**, `get_tx_ancenstors` now computes `cell_ref_parents` = all 2000 cell_dep transactions, giving `ancestors_count = 2002`. The condition at [6](#0-5)  evaluates `2002 - 2000 = 2 <= 1000`, entering the eviction path. The loop at [7](#0-6)  evicts 1002 transactions (decrementing `ancestors_count` by 1 per `remove_entry_and_descendants` call). The test confirms the first 1002 cell_dep transactions are marked `Rejected`: [8](#0-7) 

The `assert!` at [9](#0-8)  does not panic in this scenario because after evicting 1002 entries, `ancestors.len() = 999 < 1000`. The assert panic sub-claim is not concretely demonstrated and is not validated here.

## Impact Explanation

**High — Vulnerabilities or bad designs which could cause CKB network congestion with few costs.**

An attacker paying only normal fees can submit thousands of small cell_dep transactions (bounded only by `max_tx_pool_size`, default 180 MB), filling the pool and denying space to legitimate users. Additionally, the attacker can trigger mass eviction of legitimate in-pool transactions by submitting a single input-consuming transaction, forcing those users to resubmit. Both effects are reachable cheaply and repeatably by any unprivileged `send_transaction` caller.

## Likelihood Explanation

No special privileges are required. The attack requires three steps: (1) submit one transaction creating a cell, (2) submit N transactions using that cell as a `cell_dep` (each passes all admission checks individually since `ancestors_count = 2 <= 1000`), (3) submit one transaction consuming that cell as an input. The default `max_ancestors_count` is 1000 and `max_tx_pool_size` is 180 MB, so at minimum fee rate an attacker can sustain thousands of pool-filling transactions. The behavior is confirmed by an existing integration test in the repository.

## Recommendation

1. **Count cell_dep ancestors toward the admission limit**: In `check_and_record_ancestors`, count all in-pool parents (both input-based and cell_dep-based) toward `max_ancestors_count` at admission time, not only at eviction time.
2. **Bound the number of in-pool transactions referencing any single out_point as a cell_dep**: Add a `max_cell_dep_ref_count` limit in `Edges::insert_deps` to reject new transactions when too many in-pool transactions already reference the same out_point as a cell_dep.
3. **Replace the `assert!` at L636 with a graceful error**: Return `Err(Reject::ExceededMaximumAncestorsCount)` instead of panicking, to prevent any future code path from crashing the node process.

## Proof of Concept

The integration test `TxPoolLimitAncestorCount` in `test/src/specs/tx_pool/limit.rs` is a direct, self-contained reproduction:

- 2000 cell_dep transactions referencing `tx_a` are submitted and all accepted (`assert!(res.is_ok())` at L99), despite `max_ancestors_count = 1000`. [5](#0-4) 
- A transaction consuming `tx_a`'s output as an input is submitted and accepted (L116), triggering eviction. [10](#0-9) 
- The test asserts that the first 1002 cell_dep transactions are `Rejected` (L123–125), confirming forced eviction of legitimate transactions. [8](#0-7)

### Citations

**File:** tx-pool/src/component/pool_map.rs (L525-553)
```rust
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

**File:** tx-pool/src/component/pool_map.rs (L616-625)
```rust
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

**File:** test/src/specs/tx_pool/limit.rs (L113-116)
```rust
        let res = node0
            .rpc_client()
            .send_transaction_result(last.data().into());
        assert!(res.is_ok());
```

**File:** test/src/specs/tx_pool/limit.rs (L119-126)
```rust
        for (i, tx) in cell_ref_txs.iter().enumerate() {
            let res = node0
                .rpc_client()
                .get_transaction_with_verbosity(tx.hash(), 2);
            if i < 1002 {
                assert!(matches!(res.tx_status.status, Status::Rejected));
            }
        }
```

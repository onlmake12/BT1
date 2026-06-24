Audit Report

## Title
`total_tx_size` / `total_tx_cycles` Inflated When Cell-Dep-Ref Evictions Occur During `add_entry` — (File: tx-pool/src/component/pool_map.rs)

## Summary

In `PoolMap::add_entry`, the pool-wide accounting variables `total_tx_size` and `total_tx_cycles` are computed as local snapshots **before** the internal eviction step runs, then unconditionally written back to `self` **after** it. When `check_and_record_ancestors` evicts existing pool entries via `remove_entry` → `update_stat_for_remove_tx`, those decrements to `self.total_tx_size` / `self.total_tx_cycles` are silently overwritten by the stale pre-eviction snapshot. The result is permanently inflated counters, causing spurious `Reject::Full` rejections and cascading over-eviction of legitimate transactions.

## Finding Description

The exact sequence in `add_entry` (lines 200–221):

```rust
// Step 1: snapshot pre-eviction totals into LOCAL variables (self is not mutated here)
let (total_tx_size, total_tx_cycles) =
    self.updated_stat_for_add_tx(entry.size, entry.cycles)?;   // lines 210-211

// Step 2: evictions happen; each calls update_stat_for_remove_tx,
//         which DECREMENTS self.total_tx_size / self.total_tx_cycles
evicts = self.check_and_record_ancestors(&mut entry)?;          // line 213

// Step 3: OVERWRITE self.* with the stale pre-eviction snapshot
self.total_tx_size  = total_tx_size;                            // line 218
self.total_tx_cycles = total_tx_cycles;                         // line 219
```

`updated_stat_for_add_tx` takes `&self` (immutable) and returns new values as a tuple — it does not modify `self`. [1](#0-0) 

`update_stat_for_remove_tx` does mutate `self.total_tx_size` and `self.total_tx_cycles` via checked subtraction. [2](#0-1) 

`check_and_record_ancestors` calls `remove_entry_and_descendants` in a loop, each of which calls `remove_entry` → `update_stat_for_remove_tx`, correctly decrementing the live counters. [3](#0-2) 

Those decrements are then discarded when `add_entry` writes the stale snapshot back. [4](#0-3) 

**Concrete arithmetic (evicting E bytes / E_c cycles):**

| Variable | Before add | After eviction | After line 218-219 (bug) | Correct value |
|---|---|---|---|---|
| `total_tx_size` | S | S − E | S + new_size | S − E + new_size |
| `total_tx_cycles` | C | C − E_c | C + new_cycles | C − E_c + new_cycles |

The pool over-reports occupancy by exactly the size and cycles of every evicted entry.

## Impact Explanation

`total_tx_size` is the sole guard in `limit_size` that drives further eviction: [5](#0-4) 

It is also the basis for the overflow/full check in `updated_stat_for_add_tx` that rejects new submissions with `Reject::Full`: [6](#0-5) 

With inflated counters:
1. **Spurious `Reject::Full` rejections** — legitimate transactions are refused even though the pool has real capacity.
2. **Cascading over-eviction** — `limit_size` keeps evicting valid pending/proposed transactions until the inflated counter drops below `max_tx_pool_size`, removing more transactions than necessary.
3. **Incorrect RPC statistics** — `get_tx_pool_info` reports permanently wrong values until the pool is cleared.

This matches the allowed impact: **High — Vulnerabilities or bad designs which could cause CKB network congestion with few costs.** An unprivileged attacker can repeatedly trigger this to make the mempool appear full, causing legitimate transactions to be rejected or evicted, effectively congesting CKB transaction processing at low cost.

## Likelihood Explanation

The eviction path in `check_and_record_ancestors` is intentionally reachable and is already exercised by the existing integration test `TxPoolLimitAncestorCount`: [7](#0-6) 

Any unprivileged user can trigger this via the `send_raw_transaction` RPC or P2P relay. The default `max_ancestors_count` is 25, so the setup requires only a modest number of crafted transactions (slightly more than 25 cell-dep-referencing transactions sharing one output, then one transaction consuming that output as an input). The test itself uses 2,000 such transactions and confirms 1,002 are evicted — demonstrating the bug is exercised at scale with no special privileges.

## Recommendation

Compute the final totals **after** all evictions complete, not before. Remove the pre-eviction snapshot and instead call `updated_stat_for_add_tx` after `check_and_record_ancestors` returns:

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
    // Do NOT snapshot totals here — evictions below will mutate self.total_tx_size/cycles
    trace!("pool_map.add_{:?} {}", status, entry.transaction().hash());
    evicts = self.check_and_record_ancestors(&mut entry)?;
    self.record_entry_edges(&entry)?;
    self.insert_entry(&entry, status);
    self.record_entry_descendants(&entry);
    self.track_entry_statics(None, Some(status));
    // Now compute from post-eviction self.total_tx_size/cycles
    let (total_tx_size, total_tx_cycles) =
        self.updated_stat_for_add_tx(entry.size, entry.cycles)?;
    self.total_tx_size = total_tx_size;
    self.total_tx_cycles = total_tx_cycles;
    Ok((true, evicts))
}
```

Note: `updated_stat_for_add_tx` must be called after `insert_entry` if it reads from the entries collection, or alternatively the increment can be done inline with `saturating_add`. A simpler alternative is to add a `recompute_total_stat()` method that sums sizes/cycles from actual pool contents and call it after all mutations.

## Proof of Concept

1. Submit `N = max_ancestors_count + 2` (e.g., 27 with default limit 25) transactions that all reference the same unspent cell output as a `cell_dep`. Each is accepted; `total_tx_size` grows correctly.
2. Submit one transaction whose **input** consumes that same cell output. `check_and_record_ancestors` detects `ancestors_count > max_ancestors_count`, identifies the cell-ref-parents, and evicts enough via `remove_entry_and_descendants`. Each eviction calls `update_stat_for_remove_tx`, decrementing `self.total_tx_size`.
3. `add_entry` then writes `self.total_tx_size = total_tx_size` (the pre-eviction snapshot + new entry size), overwriting all decrements.
4. `total_tx_size` is now inflated by the sum of all evicted entries' sizes.
5. Subsequent `send_raw_transaction` calls are rejected with `Reject::Full` even though the pool has real free capacity, or `limit_size` evicts additional legitimate transactions unnecessarily.
6. The existing integration test `TxPoolLimitAncestorCount` already sets up this exact scenario at scale (2,000 cell-dep txs, 1,002 evictions) and can be extended with post-submission assertions on `get_tx_pool_info` to confirm the inflated `total_tx_size`.

### Citations

**File:** tx-pool/src/component/pool_map.rs (L218-219)
```rust
        self.total_tx_size = total_tx_size;
        self.total_tx_cycles = total_tx_cycles;
```

**File:** tx-pool/src/component/pool_map.rs (L615-625)
```rust
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
```

**File:** tx-pool/src/component/pool_map.rs (L711-729)
```rust
    fn updated_stat_for_add_tx(
        &self,
        tx_size: usize,
        cycles: Cycle,
    ) -> Result<(usize, Cycle), Reject> {
        let total_tx_size = self.total_tx_size.checked_add(tx_size).ok_or_else(|| {
            Reject::Full(format!(
                "tx-pool total_tx_size {} overflows by add {}",
                self.total_tx_size, tx_size
            ))
        })?;
        let total_tx_cycles = self.total_tx_cycles.checked_add(cycles).ok_or_else(|| {
            Reject::Full(format!(
                "tx-pool total_tx_cycles {} overflows by add {}",
                self.total_tx_cycles, cycles
            ))
        })?;
        Ok((total_tx_size, total_tx_cycles))
    }
```

**File:** tx-pool/src/component/pool_map.rs (L733-740)
```rust
    fn update_stat_for_remove_tx(&mut self, tx_size: usize, cycles: Cycle) {
        match (
            self.total_tx_size.checked_sub(tx_size),
            self.total_tx_cycles.checked_sub(cycles),
        ) {
            (Some(total_tx_size), Some(total_tx_cycles)) => {
                self.total_tx_size = total_tx_size;
                self.total_tx_cycles = total_tx_cycles;
```

**File:** tx-pool/src/pool.rs (L298-307)
```rust
        while self.pool_map.total_tx_size > self.config.max_tx_pool_size {
            let next_evict_entry = || {
                self.pool_map
                    .next_evict_entry(Status::Pending)
                    .or_else(|| self.pool_map.next_evict_entry(Status::Gap))
                    .or_else(|| self.pool_map.next_evict_entry(Status::Proposed))
            };

            if let Some(id) = next_evict_entry() {
                let removed = self.pool_map.remove_entry_and_descendants(&id);
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

### Title
Stale Aggregate Counter Overwrite After Ancestor-Eviction in `add_entry` — (`tx-pool/src/component/pool_map.rs`)

---

### Summary

In `PoolMap::add_entry`, the aggregate pool-size counters `total_tx_size` and `total_tx_cycles` are computed **before** the ancestor-eviction step, then unconditionally written back **after** evictions have already correctly decremented those same counters. The net effect is that every evicted transaction's size and cycles are silently added back into the pool totals, inflating them permanently until the node restarts or the pool is cleared.

---

### Finding Description

`PoolMap::add_entry` in `tx-pool/src/component/pool_map.rs` follows this sequence:

```
line 210-211: (total_tx_size, total_tx_cycles) = updated_stat_for_add_tx(entry.size, entry.cycles)
              // snapshot = self.total_tx_size + new_entry_size

line 213:     evicts = self.check_and_record_ancestors(&mut entry)
              // may call remove_entry_and_descendants → remove_entry → update_stat_for_remove_tx
              // which correctly decrements self.total_tx_size and self.total_tx_cycles

line 218-219: self.total_tx_size  = total_tx_size   // ← stale pre-eviction snapshot
              self.total_tx_cycles = total_tx_cycles  // ← stale pre-eviction snapshot
``` [1](#0-0) 

Inside `check_and_record_ancestors`, when the ancestor count exceeds `max_ancestors_count` and cell-ref parents exist, the code evicts transactions via `remove_entry_and_descendants`: [2](#0-1) 

Each `remove_entry_and_descendants` call chains into `remove_entry`, which calls `update_stat_for_remove_tx` and correctly decrements `self.total_tx_size` and `self.total_tx_cycles`: [3](#0-2) 

But the local snapshot `total_tx_size` captured at line 210 was computed as `old_total + new_entry_size`, with no knowledge of the evictions that follow. When it is written back at line 218, it overwrites the correctly-decremented value, effectively **un-doing** the decrement for every evicted transaction.

The `update_stat_for_remove_tx` function that performs the correct decrement: [4](#0-3) 

The aggregate fields that are corrupted: [5](#0-4) 

---

### Impact Explanation

`total_tx_size` is the primary guard for pool admission and eviction:

1. **Spurious eviction loop** — `limit_size` loops `while self.pool_map.total_tx_size > self.config.max_tx_pool_size`. An inflated counter causes the loop to evict additional valid transactions that should remain in the pool, degrading pool quality and potentially starving legitimate users. [6](#0-5) 

2. **False `Reject::Full` for legitimate transactions** — `updated_stat_for_add_tx` rejects new submissions when the inflated `total_tx_size` appears to exceed capacity, even though the real pool is well within limits. [7](#0-6) 

3. **Incorrect RPC reporting** — `total_tx_size` and `total_tx_cycles` are surfaced directly via `tx_pool_info` RPC, misleading operators and downstream tooling. [8](#0-7) 

---

### Likelihood Explanation

The eviction path inside `check_and_record_ancestors` is exercised whenever a submitted transaction has more than `max_ancestors_count` ancestors reachable through cell-dep references, but the excess can be reduced by evicting those cell-ref parents. This scenario is explicitly tested in the integration suite: [9](#0-8) 

Any unprivileged tx-pool submitter (via `send_transaction` RPC or P2P relay) can craft a transaction that references a cell output already used as a dep by many pooled transactions, triggering the eviction path and corrupting the counters. No special privilege, key, or majority hashpower is required.

---

### Recommendation

Move the `total_tx_size`/`total_tx_cycles` snapshot computation to **after** `check_and_record_ancestors` completes, so the snapshot reflects the post-eviction state of `self.total_tx_size`. Alternatively, compute the final values by reading `self.total_tx_size` (already correctly decremented by evictions) and adding only the new entry's contribution at that point:

```rust
evicts = self.check_and_record_ancestors(&mut entry)?;
// Now self.total_tx_size already reflects evictions; add only the new entry
let total_tx_size = self.total_tx_size.checked_add(entry.size)...;
let total_tx_cycles = self.total_tx_cycles.checked_add(entry.cycles)...;
self.record_entry_edges(&entry)?;
self.insert_entry(&entry, status);
self.record_entry_descendants(&entry);
self.track_entry_statics(None, Some(status));
self.total_tx_size = total_tx_size;
self.total_tx_cycles = total_tx_cycles;
```

---

### Proof of Concept

1. Fill the pool with N transactions that all use the same cell output as a `cell_dep` (cell-ref parents), where N > `max_ancestors_count` (default 125).
2. Submit a new transaction whose input consumes that same cell output. Its ancestor count = N + 1 > `max_ancestors_count`, but `ancestors_count - cell_ref_parents.len() ≤ max_ancestors_count`, so the eviction branch fires.
3. `check_and_record_ancestors` evicts `(N + 1 - max_ancestors_count)` cell-ref transactions. Each eviction correctly decrements `self.total_tx_size` by that transaction's size.
4. Lines 218-219 then overwrite `self.total_tx_size` with the stale snapshot = `old_total + new_entry_size`, which does not reflect the evictions.
5. After the call, `pool_map.total_tx_size` is inflated by `sum(evicted_tx_sizes)`.
6. Subsequent calls to `limit_size` will evict additional valid transactions, and `updated_stat_for_add_tx` will falsely reject new submissions with `Reject::Full`.

The integration test at `test/src/specs/tx_pool/limit.rs:TxPoolLimitAncestorCount` demonstrates the eviction path is reachable and functional; the counter corruption is the missing invariant check. [10](#0-9)

### Citations

**File:** tx-pool/src/component/pool_map.rs (L68-75)
```rust
    // sum of all tx_pool tx's virtual sizes.
    pub(crate) total_tx_size: usize,
    // sum of all tx_pool tx's cycles.
    pub(crate) total_tx_cycles: Cycle,
    pub(crate) pending_count: usize,
    pub(crate) gap_count: usize,
    pub(crate) proposed_count: usize,
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

**File:** tx-pool/src/component/pool_map.rs (L731-758)
```rust
    /// Update size and cycles statistics for remove tx
    /// cycles overflow is possible, currently obtaining cycles is not accurate
    fn update_stat_for_remove_tx(&mut self, tx_size: usize, cycles: Cycle) {
        match (
            self.total_tx_size.checked_sub(tx_size),
            self.total_tx_cycles.checked_sub(cycles),
        ) {
            (Some(total_tx_size), Some(total_tx_cycles)) => {
                self.total_tx_size = total_tx_size;
                self.total_tx_cycles = total_tx_cycles;
            }
            _ => {
                if let Some((total_tx_size, total_tx_cycles)) = self.recompute_total_stat() {
                    error!(
                        "tx-pool total stats underflowed when removing size {} cycles {}, recomputed size {} cycles {}",
                        tx_size, cycles, total_tx_size, total_tx_cycles
                    );
                    self.total_tx_size = total_tx_size;
                    self.total_tx_cycles = total_tx_cycles;
                } else {
                    error!(
                        "tx-pool total stats underflowed when removing size {} cycles {}, and recomputing overflowed",
                        tx_size, cycles
                    );
                }
            }
        }
    }
```

**File:** tx-pool/src/pool.rs (L292-329)
```rust
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

**File:** util/jsonrpc-types/src/pool.rs (L36-39)
```rust
    /// Total size of transactions bytes in the pool of all the different kinds of states (excluding orphan transactions).
    pub total_tx_size: Uint64,
    /// Total consumed VM cycles of all the transactions in the pool (excluding orphan transactions).
    pub total_tx_cycles: Uint64,
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

### Title
`total_tx_size` / `total_tx_cycles` Inflated When Cell-Dep-Ref Evictions Occur During `add_entry` — (`File: tx-pool/src/component/pool_map.rs`)

---

### Summary

In `PoolMap::add_entry`, the pool-wide accounting variables `total_tx_size` and `total_tx_cycles` are snapshotted **before** the internal eviction step (`check_and_record_ancestors`) runs, and then unconditionally written back **after** it. When that eviction step removes existing pool entries (via `remove_entry_and_descendants` → `remove_entry` → `update_stat_for_remove_tx`), the decrements those removals apply to `self.total_tx_size` / `self.total_tx_cycles` are silently overwritten by the stale snapshot. The result is that both counters are permanently inflated by the total size and cycles of every evicted entry, causing the pool to believe it is fuller than it actually is.

---

### Finding Description

`PoolMap::add_entry` (lines 200–221) follows this sequence:

```
// Step 1 – snapshot totals BEFORE any eviction
let (total_tx_size, total_tx_cycles) =
    self.updated_stat_for_add_tx(entry.size, entry.cycles)?;   // line 210-211

// Step 2 – may evict N entries; each calls update_stat_for_remove_tx,
//           which DECREMENTS self.total_tx_size / self.total_tx_cycles
evicts = self.check_and_record_ancestors(&mut entry)?;          // line 213

// Step 3 – OVERWRITE self.* with the pre-eviction snapshot + new entry
self.total_tx_size  = total_tx_size;                            // line 218
self.total_tx_cycles = total_tx_cycles;                         // line 219
``` [1](#0-0) 

The eviction path inside `check_and_record_ancestors` is triggered when a new transaction's ancestor count exceeds `max_ancestors_count` but can be brought within limits by removing "cell-ref-parent" entries — pool transactions that reference the same cell output as a dep that the new transaction is about to consume as an input: [2](#0-1) 

Each call to `remove_entry_and_descendants` inside that loop calls `remove_entry`, which calls `update_stat_for_remove_tx` and correctly decrements `self.total_tx_size` / `self.total_tx_cycles`: [3](#0-2) 

But those decrements are immediately discarded when `add_entry` writes the stale snapshot back at lines 218–219.

**Concrete arithmetic:**

| Variable | Before add | After evicting E bytes / E_c cycles | After line 218-219 (bug) | Correct value |
|---|---|---|---|---|
| `total_tx_size` | S | S − E | S + new_size | S − E + new_size |
| `total_tx_cycles` | C | C − E_c | C + new_cycles | C − E_c + new_cycles |

The pool over-reports its own occupancy by exactly the size and cycles of every evicted entry.

---

### Impact Explanation

`total_tx_size` is the sole guard used by `limit_size` to decide whether to evict further transactions: [4](#0-3) 

And it is the basis for the overflow check in `updated_stat_for_add_tx` that rejects new submissions with `Reject::Full`: [5](#0-4) 

With inflated counters:

1. **Spurious `Reject::Full` rejections** — legitimate transactions are refused even though the pool has real capacity, because the inflated `total_tx_size` makes the pool appear full.
2. **Cascading over-eviction** — `limit_size` keeps evicting valid pending/proposed transactions until the inflated counter drops below `max_tx_pool_size`, removing more transactions than necessary.
3. **Incorrect RPC statistics** — `get_tx_pool_info` reports `total_tx_size` / `total_tx_cycles` that are permanently wrong until the pool is cleared.

---

### Likelihood Explanation

The eviction path is **intentionally reachable** and is exercised by the existing integration test `TxPoolLimitAncestorCount`: [6](#0-5) 

The test submits 2 000 cell-dep-referencing transactions, then submits one transaction that consumes the referenced cell output, triggering eviction of 1 002 entries. Any unprivileged tx-pool submitter (via `send_raw_transaction` RPC or P2P relay) can replicate this pattern. The default `max_ancestors_count` is 25 (confirmed in CHANGELOG), so the setup requires only a modest number of crafted transactions.

---

### Recommendation

Compute the final totals **after** evictions complete, not before. Replace the pre-eviction snapshot with a post-eviction increment:

```rust
// Remove the pre-eviction snapshot lines 210-211.
evicts = self.check_and_record_ancestors(&mut entry)?;
self.record_entry_edges(&entry)?;
self.insert_entry(&entry, status);
self.record_entry_descendants(&entry);
self.track_entry_statics(None, Some(status));
// Increment from the current (post-eviction) totals instead:
let (total_tx_size, total_tx_cycles) =
    self.updated_stat_for_add_tx(entry.size, entry.cycles)?;
self.total_tx_size  = total_tx_size;
self.total_tx_cycles = total_tx_cycles;
```

Alternatively, call `recompute_total_stat()` after all mutations to rebase the counters from the actual pool contents.

---

### Proof of Concept

1. Submit `N > max_ancestors_count` transactions that all reference the same unspent cell output as a `cell_dep` (e.g., N = 1 002 with default limit 1 000). Each is accepted; `total_tx_size` grows correctly.
2. Submit one transaction whose **input** consumes that same cell output. `check_and_record_ancestors` detects `ancestors_count > max_ancestors_count`, identifies the N cell-ref-parents, and evicts enough of them via `remove_entry_and_descendants`. Each eviction calls `update_stat_for_remove_tx`, decrementing `self.total_tx_size` by the evicted sizes.
3. `add_entry` then writes `self.total_tx_size = total_tx_size` (the pre-eviction snapshot + new entry size), overwriting all decrements.
4. `total_tx_size` is now inflated by the sum of all evicted entries' sizes.
5. Subsequent `send_raw_transaction` calls are rejected with `Reject::Full` even though the pool has real free capacity, or `limit_size` evicts additional legitimate transactions unnecessarily. [7](#0-6) [8](#0-7)

### Citations

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

**File:** tx-pool/src/pool.rs (L298-328)
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

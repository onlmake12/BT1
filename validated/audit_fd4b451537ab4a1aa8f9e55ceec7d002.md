### Title
`total_tx_size` / `total_tx_cycles` Overestimated After Cell-Dep Ancestor Eviction in `add_entry` — (`tx-pool/src/component/pool_map.rs`)

---

### Summary

In `PoolMap::add_entry`, the aggregate pool-size counters `total_tx_size` and `total_tx_cycles` are snapshotted **before** `check_and_record_ancestors` may evict transactions, then unconditionally written back **after** the evictions. The evictions correctly decrement the counters inside `remove_entry`, but those decrements are immediately overwritten by the stale snapshot. The result is a permanent overestimation of pool size that causes `limit_size` to expel legitimate transactions from the pool.

---

### Finding Description

`PoolMap::add_entry` follows this sequence:

```rust
// Step 1 – snapshot new totals BEFORE any eviction
let (total_tx_size, total_tx_cycles) =
    self.updated_stat_for_add_tx(entry.size, entry.cycles)?;   // line 210-211

// Step 2 – may call remove_entry_and_descendants → update_stat_for_remove_tx
//           which DECREMENTS self.total_tx_size / self.total_tx_cycles
evicts = self.check_and_record_ancestors(&mut entry)?;          // line 213

// Step 3 – unconditionally OVERWRITE with the pre-eviction snapshot
self.total_tx_size  = total_tx_size;                            // line 218
self.total_tx_cycles = total_tx_cycles;                         // line 219
``` [1](#0-0) 

The eviction path inside `check_and_record_ancestors` is reached when a new transaction's ancestor count exceeds `max_ancestors_count` but the excess is entirely composed of *cell-ref parents* (transactions that reference the same cell dep as the new transaction's inputs). In that case the loop calls `remove_entry_and_descendants`, which calls `remove_entry`, which calls `update_stat_for_remove_tx` and correctly decrements `self.total_tx_size`: [2](#0-1) [3](#0-2) [4](#0-3) 

After `check_and_record_ancestors` returns, `self.total_tx_size` is the correct post-eviction value. But line 218 immediately overwrites it with the pre-eviction snapshot (`old_total + entry.size`), erasing the decrements. The overestimation equals the sum of sizes of all evicted entries.

---

### Impact Explanation

`total_tx_size` is the sole guard used by `limit_size` to decide whether to expel further transactions:

```rust
while self.pool_map.total_tx_size > self.config.max_tx_pool_size {
    // evict lowest-fee transactions
}
``` [5](#0-4) 

After the overestimation, `limit_size` believes the pool is larger than it actually is and evicts additional legitimate pending/proposed transactions. Each evicted transaction is permanently rejected with `Reject::Full` and reported to the submitter. The pool's actual byte occupancy is lower than `max_tx_pool_size`, so the evictions are spurious. Repeated triggering inflates the counter further, compounding the effect.

`total_tx_size` is also surfaced verbatim through the `get_tx_pool_info` RPC, so operators and monitoring tools see an inflated pool size. [6](#0-5) 

---

### Likelihood Explanation

The eviction path is explicitly exercised by the integration test `TxPoolLimitAncestorCount`, which submits 2 000 cell-dep-referencing transactions and then one transaction that consumes the cell dep, triggering eviction of 1 002 entries: [7](#0-6) 

Any unprivileged RPC caller or P2P transaction relay can replicate this pattern:

1. Submit N transactions that each reference the same in-pool output as a `cell_dep` (N > `max_ancestors_count`, default 1 000).
2. Submit one transaction whose input *consumes* that output.

Step 2 triggers the eviction branch and leaves `total_tx_size` inflated by the aggregate size of the evicted transactions. The attacker does not need any privileged access; standard `send_transaction` RPC calls suffice.

---

### Recommendation

Move the counter snapshot to **after** `check_and_record_ancestors` completes, so it reflects the post-eviction state:

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
    // Validate that adding entry won't overflow (but don't commit yet)
    self.updated_stat_for_add_tx(entry.size, entry.cycles)?;

    evicts = self.check_and_record_ancestors(&mut entry)?;
    self.record_entry_edges(&entry)?;
    self.insert_entry(&entry, status);
    self.record_entry_descendants(&entry);
    self.track_entry_statics(None, Some(status));

    // Compute final totals AFTER evictions have already decremented the counters
    let (total_tx_size, total_tx_cycles) =
        self.updated_stat_for_add_tx(entry.size, entry.cycles)?;
    self.total_tx_size  = total_tx_size;
    self.total_tx_cycles = total_tx_cycles;
    Ok((true, evicts))
}
```

Alternatively, accumulate the evicted sizes during the loop and subtract them from the pre-computed snapshot before assigning.

---

### Proof of Concept

**Setup (pseudo-steps via `send_transaction` RPC):**

1. Mine a block containing `tx_root` with one output `(tx_root, 0)`.
2. Submit 1 001 independent transactions `cell_ref_1 … cell_ref_1001`, each spending a distinct UTXO **and** declaring `cell_dep = (tx_root, 0)`. All are accepted (each has 0 ancestors).
3. Submit `consumer` whose single input is `(tx_root, 0)` (spending the cell dep output).

**What happens inside `add_entry` for `consumer`:**
- `ancestors` = {`cell_ref_1`, …, `cell_ref_1001`} → `ancestors_count` = 1 002 > 1 000.
- `cell_ref_parents.len()` = 1 001 → `1002 - 1001 = 1 ≤ 1000` → eviction branch entered.
- Loop evicts 2 entries (to bring count to 1 000): `remove_entry_and_descendants` decrements `self.total_tx_size` by their sizes.
- Line 218 overwrites `self.total_tx_size` with the pre-eviction snapshot, inflating it by those 2 entries' sizes.

**Observable effect:**
- `get_tx_pool_info` returns `total_tx_size` larger than the actual sum of entry sizes.
- If the pool was near `max_tx_pool_size`, `limit_size` immediately evicts additional legitimate transactions even though actual occupancy is below the limit. [8](#0-7) [9](#0-8)

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

**File:** tx-pool/src/component/pool_map.rs (L235-249)
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

**File:** tx-pool/src/component/pool_map.rs (L733-758)
```rust
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

**File:** util/types/src/core/tx_pool.rs (L336-337)
```rust
    pub total_tx_size: usize,
    /// Total consumed VM cycles of all the transactions in the pool.
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

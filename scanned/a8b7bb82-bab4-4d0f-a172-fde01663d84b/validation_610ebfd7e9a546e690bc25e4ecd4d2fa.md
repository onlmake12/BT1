### Title
`total_tx_size` / `total_tx_cycles` Inflated When Ancestor-Limit Evictions Occur Inside `add_entry` — (`tx-pool/src/component/pool_map.rs`)

---

### Summary

`PoolMap::add_entry` pre-computes the new `total_tx_size` / `total_tx_cycles` values before calling `check_and_record_ancestors`. That inner call can evict existing pool entries (via `remove_entry_and_descendants` → `update_stat_for_remove_tx`), which correctly subtracts the evicted sizes from `self.total_tx_size` in place. However, `add_entry` then unconditionally overwrites `self.total_tx_size` with the stale pre-eviction value, permanently inflating the counter by the size of every evicted entry. An unprivileged tx-pool submitter can trigger this path repeatedly to make the pool believe it is full when it is not, causing `limit_size` to evict legitimate pending transactions and causing new submissions to be rejected with `Reject::Full`.

---

### Finding Description

In `add_entry` the sequence is:

```rust
// Step 1 – snapshot new totals BEFORE any eviction
let (total_tx_size, total_tx_cycles) =
    self.updated_stat_for_add_tx(entry.size, entry.cycles)?;   // local vars

// Step 2 – may evict entries; each eviction calls update_stat_for_remove_tx
//           which modifies self.total_tx_size IN PLACE
evicts = self.check_and_record_ancestors(&mut entry)?;

// Step 3 – overwrites self.total_tx_size with the STALE pre-eviction snapshot
self.total_tx_size = total_tx_size;
self.total_tx_cycles = total_tx_cycles;
``` [1](#0-0) 

`check_and_record_ancestors` evicts entries when a new transaction's ancestor count exceeds `max_ancestors_count` but can be reduced by removing cell-dep-referencing parents: [2](#0-1) 

Each evicted entry flows through `remove_entry` → `update_stat_for_remove_tx`, which subtracts the evicted size from `self.total_tx_size`: [3](#0-2) [4](#0-3) 

After those subtractions, `add_entry` blindly restores `self.total_tx_size` to `old_total + entry.size`, ignoring the evictions. The correct value is `old_total − evicted_size + entry.size`; the actual value is `old_total + entry.size`. The counter is inflated by `evicted_size` per triggering call.

The codebase already acknowledges a related ordering hazard with a `FIXME` comment at the same function: [5](#0-4) 

---

### Impact Explanation

`limit_size` is called immediately after every successful `_submit_entry` and evicts pool entries until `total_tx_size ≤ max_tx_pool_size`: [6](#0-5) 

With an inflated `total_tx_size`, `limit_size` evicts legitimate pending/proposed transactions that should not have been removed. Simultaneously, `updated_stat_for_add_tx` rejects new submissions with `Reject::Full` even when the pool has physical space: [7](#0-6) 

The net effect is a sustained, attacker-driven eviction of honest users' transactions from the mempool, preventing them from being proposed or committed.

---

### Likelihood Explanation

The eviction path in `check_and_record_ancestors` is reachable by any unprivileged `send_transaction` RPC caller. The attacker's recipe:

1. Submit `N > max_ancestors_count` (default 1000) transactions that all reference the same cell dep output — these become `cell_ref_parents`.
2. Submit one transaction that **consumes** that cell dep output. This triggers the eviction branch because `ancestors_count − cell_ref_parents.len() ≤ max_ancestors_count`.
3. Each such round inflates `total_tx_size` by the aggregate size of the evicted entries.
4. Repeat with a fresh cell dep to accumulate inflation across rounds.

The integration test `TxPoolLimitAncestorCount` demonstrates that 2000 cell-dep transactions can be submitted and that submitting a consumer evicts 1002 of them in one shot: [8](#0-7) 

The attacker pays transaction fees proportional to the number of submissions, but each round evicts a large batch of legitimate transactions and inflates the counter by hundreds of kilobytes. The default `max_tx_pool_size` is ~20 MB; a few dozen rounds suffice to push `total_tx_size` past the limit.

---

### Recommendation

Move the `total_tx_size` / `total_tx_cycles` assignment to **after** `check_and_record_ancestors` completes, reading the current (post-eviction) `self.total_tx_size` at that point and adding only `entry.size`:

```rust
evicts = self.check_and_record_ancestors(&mut entry)?;
self.record_entry_edges(&entry)?;
self.insert_entry(&entry, status);
self.record_entry_descendants(&entry);
self.track_entry_statics(None, Some(status));
// Compute totals AFTER evictions have already updated self.total_tx_size
self.total_tx_size = self.total_tx_size.saturating_add(entry.size);
self.total_tx_cycles = self.total_tx_cycles.saturating_add(entry.cycles);
```

Alternatively, call `updated_stat_for_add_tx` after `check_and_record_ancestors` so the snapshot is taken from the already-adjusted `self.total_tx_size`.

---

### Proof of Concept

**Setup**: node with `min_fee_rate = 0`, `max_tx_pool_size = 2_000_000` (2 MB), `max_ancestors_count = 1000`.

**Round (repeat ~15 times)**:
1. Mine a coinbase cell.
2. Submit 1001 independent transactions each carrying `cell_dep` pointing to the same unspent output `O`.
3. Submit one transaction whose **input** spends `O`. This triggers `check_and_record_ancestors` eviction of ≥1 cell-dep parent.
4. `add_entry` overwrites `self.total_tx_size` with the pre-eviction snapshot, inflating it by the size of the evicted entry (~200 bytes × evicted count ≈ 200 KB per round).

**After ~15 rounds**: `total_tx_size` exceeds `max_tx_pool_size` (2 MB). `limit_size` evicts all remaining honest transactions. Subsequent `send_transaction` calls from honest users receive `PoolIsFull (-1106)` even though the pool holds almost no transactions. [9](#0-8) [10](#0-9)

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

**File:** tx-pool/src/component/pool_map.rs (L582-587)
```rust
    /// Check ancestors and record for entry
    // FIXME: In the scenario that a transaction passed all RBF rules, and then removed the conflicted
    // transaction in txpool, then failed with max ancestor limits, we now need to rollback the removing.
    // this is not an issue currently, because RBF have a rule that not allow any unknown inputs except
    // the conflicted inputs, so the new transaction can not be in a long transaction chain.
    // but it's still safer to report an error before any writing kind of operation.
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

**File:** tx-pool/src/component/pool_map.rs (L711-728)
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
```

**File:** tx-pool/src/component/pool_map.rs (L733-757)
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
```

**File:** tx-pool/src/pool.rs (L292-328)
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
```

**File:** test/src/specs/tx_pool/limit.rs (L70-157)
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

        // create a transaction chain
        let input_c = &initial_inputs[2001];
        // Commit transaction root
        let tx_c = {
            let tx_c = always_success_transaction(node0, input_c);
            node0.submit_transaction(&tx_c);
            tx_c
        };

        let mut prev = tx_c;
        // Create transaction chain
        for i in 0..1000 {
            let input =
                CellMetaBuilder::from_cell_output(prev.output(0).unwrap(), Default::default())
                    .out_point(OutPoint::new(prev.hash(), 0))
                    .build();
            let cur = always_success_transaction(node0, &input);
            let res = node0
                .rpc_client()
                .send_transaction_result(cur.data().into());
            prev = cur.clone();
            if i >= 999 {
                assert!(res.is_err());
                let msg = res.err().unwrap().to_string();
                assert!(msg.contains("PoolRejectedTransactionByMaxAncestorsCountLimit"));
            } else {
                assert!(res.is_ok());
            }
        }
    }
```

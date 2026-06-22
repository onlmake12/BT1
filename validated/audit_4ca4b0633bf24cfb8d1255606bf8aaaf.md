### Title
`PoolMap::add_entry` Overwrites `total_tx_size`/`total_tx_cycles` After In-Flight Evictions, Inflating the Pool-Size Tracker — (`File: tx-pool/src/component/pool_map.rs`)

---

### Summary

`PoolMap::add_entry` pre-computes the new `total_tx_size` and `total_tx_cycles` values before calling `check_and_record_ancestors`, which can itself evict existing pool entries (via `remove_entry_and_descendants`). Those evictions correctly decrement `self.total_tx_size` through `update_stat_for_remove_tx`, but the final two lines of `add_entry` then unconditionally overwrite `self.total_tx_size` and `self.total_tx_cycles` with the stale pre-computed values, erasing all decrements. The result is a permanently inflated size tracker that causes `limit_size` to evict far more legitimate transactions than the pool's actual occupancy warrants.

---

### Finding Description

`PoolMap` maintains two running totals as internal counters:

```
// sum of all tx_pool tx's virtual sizes.
pub(crate) total_tx_size: usize,
// sum of all tx_pool tx's cycles.
pub(crate) total_tx_cycles: Cycle,
``` [1](#0-0) 

`add_entry` computes the post-add totals **before** any side-effects, stores them in locals, then performs several mutating operations, and finally commits the locals back:

```rust
let (total_tx_size, total_tx_cycles) =
    self.updated_stat_for_add_tx(entry.size, entry.cycles)?;   // (A) snapshot
// ...
evicts = self.check_and_record_ancestors(&mut entry)?;          // (B) may evict
// ...
self.total_tx_size  = total_tx_size;                            // (C) overwrites
self.total_tx_cycles = total_tx_cycles;
``` [2](#0-1) 

Step **(B)** can call `remove_entry_and_descendants` → `remove_entry` → `update_stat_for_remove_tx`, which decrements `self.total_tx_size` for every evicted entry: [3](#0-2) 

Step **(C)** then blindly overwrites `self.total_tx_size` with the snapshot from **(A)**, which never accounted for those decrements. After the call returns, the tracker reads:

```
self.total_tx_size  == (old_total + new_entry_size)
actual pool size    == (old_total + new_entry_size) - Σ(evicted_sizes)
```

The inflation equals the sum of all sizes evicted during `check_and_record_ancestors`. The same overwrite applies to `total_tx_cycles`; the existing comment in `update_stat_for_remove_tx` already acknowledges the cycles figure is unreliable: [4](#0-3) 

The eviction path inside `check_and_record_ancestors` is triggered whenever a new transaction's ancestor count exceeds `max_ancestors_count` but can be reduced by evicting cell-dep-referencing parents: [5](#0-4) 

---

### Impact Explanation

`total_tx_size` is the sole metric used by `limit_size` to enforce `max_tx_pool_size`:

```rust
while self.pool_map.total_tx_size > self.config.max_tx_pool_size {
    // evict lowest-fee-rate entries
}
``` [6](#0-5) 

With an inflated tracker the pool believes it is over-capacity when it is not. `limit_size` then evicts legitimate, high-fee-rate transactions that would otherwise remain. Each eviction correctly decrements the tracker, so the inflation is eventually consumed — but only after an excess number of valid transactions have been expelled. An attacker can repeat the trigger to keep the tracker perpetually inflated, effectively preventing honest transactions from staying in the pool.

---

### Likelihood Explanation

The trigger condition is reachable by any unprivileged `send_transaction` caller:

1. Submit N > `max_ancestors_count` (default 25) transactions that all reference the same in-pool cell dep (cell-dep ancestry, not input ancestry).
2. Submit one transaction that **consumes** that cell dep.

Step 2 enters `check_and_record_ancestors`, finds `ancestors_count > max_ancestors_count`, enters the eviction branch, and removes (N − max_ancestors_count + 1) entries — inflating `total_tx_size` by their combined sizes. The integration test `TxPoolLimitAncestorCount` demonstrates this exact flow is a supported, exercised code path. [7](#0-6) 

The default `max_tx_pool_size` is 180 MB; a single attack round with 1 000 evicted 500-byte transactions inflates the tracker by ~500 KB, causing `limit_size` to expel an equivalent volume of legitimate transactions. Repeated rounds compound the effect.

---

### Recommendation

Move the stat-update commit to **after** `check_and_record_ancestors` completes, so it incorporates any evictions that occurred. Replace the pre-computed snapshot pattern with a post-eviction recomputation or a delta approach:

```rust
// After check_and_record_ancestors, recompute or adjust:
let evicted_size:  usize = evicts.iter().map(|e| e.size).sum();
let evicted_cycles: Cycle = evicts.iter().map(|e| e.cycles).sum();
self.total_tx_size  = self.total_tx_size
    .saturating_add(entry.size)
    .saturating_sub(evicted_size);
self.total_tx_cycles = self.total_tx_cycles
    .saturating_add(entry.cycles)
    .saturating_sub(evicted_cycles);
```

Alternatively, call `recompute_total_stat` after any operation that performs in-flight evictions, consistent with the fallback already used in `update_stat_for_remove_tx`. [8](#0-7) 

---

### Proof of Concept

**Setup** (`max_ancestors_count = 25`, `max_tx_pool_size = 180 MB`):

1. Submit 30 transactions T₁…T₃₀, each referencing cell dep `X` (an in-pool output). `total_tx_size` = 30 × S.
2. Submit transaction T_consume that spends output `X`.
3. `add_entry(T_consume)` runs:
   - **(A)** `total_tx_size_local = 30S + S_consume`
   - **(B)** `check_and_record_ancestors` finds 31 ancestors > 25; evicts 6 cell-dep parents (T₁…T₆). Each eviction calls `update_stat_for_remove_tx`, decrementing `self.total_tx_size` by 6S → `self.total_tx_size = 24S`.
   - **(C)** `self.total_tx_size = total_tx_size_local = 30S + S_consume`.
4. **Actual pool size** = 24S + S_consume. **Tracker** = 30S + S_consume. **Inflation** = 6S.
5. `limit_size` is called; it evicts 6S worth of legitimate transactions that should have remained.
6. Repeat from step 1 to accumulate further inflation.

### Citations

**File:** tx-pool/src/component/pool_map.rs (L68-71)
```rust
    // sum of all tx_pool tx's virtual sizes.
    pub(crate) total_tx_size: usize,
    // sum of all tx_pool tx's cycles.
    pub(crate) total_tx_cycles: Cycle,
```

**File:** tx-pool/src/component/pool_map.rs (L210-219)
```rust
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

**File:** tx-pool/src/component/pool_map.rs (L603-625)
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
```

**File:** tx-pool/src/component/pool_map.rs (L698-708)
```rust
    fn recompute_total_stat(&self) -> Option<(usize, Cycle)> {
        self.entries.iter().try_fold(
            (0usize, 0 as Cycle),
            |(total_size, total_cycles), (_, entry)| {
                Some((
                    total_size.checked_add(entry.inner.size)?,
                    total_cycles.checked_add(entry.inner.cycles)?,
                ))
            },
        )
    }
```

**File:** tx-pool/src/component/pool_map.rs (L731-733)
```rust
    /// Update size and cycles statistics for remove tx
    /// cycles overflow is possible, currently obtaining cycles is not accurate
    fn update_stat_for_remove_tx(&mut self, tx_size: usize, cycles: Cycle) {
```

**File:** tx-pool/src/pool.rs (L298-326)
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

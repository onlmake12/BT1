### Title
`PoolMap::add_entry` Overwrites Eviction-Decremented `total_tx_size`/`total_tx_cycles` With Pre-Eviction Values, Inflating Pool Size Accounting - (File: `tx-pool/src/component/pool_map.rs`)

### Summary
In `PoolMap::add_entry`, the pool-size counters `total_tx_size` and `total_tx_cycles` are pre-computed before ancestor evictions occur, then unconditionally written back after those evictions have already decremented the same counters. The evicted transactions' sizes and cycles are silently lost from the accounting, permanently inflating the pool's reported size. Any unprivileged `send_transaction` caller who triggers the cell-ref ancestor eviction path can cause the pool to believe it is full when it is not, causing legitimate subsequent transactions to be rejected or unnecessarily evicted.

---

### Finding Description

`PoolMap::add_entry` in `tx-pool/src/component/pool_map.rs` follows this sequence:

```
1. (total_tx_size, total_tx_cycles) = updated_stat_for_add_tx(entry.size, entry.cycles)
   // Snapshots: local_size  = self.total_tx_size + entry.size
   //            local_cycles = self.total_tx_cycles + entry.cycles

2. evicts = check_and_record_ancestors(&mut entry)
   // May call remove_entry_and_descendants → remove_entry → update_stat_for_remove_tx
   // This MODIFIES self.total_tx_size and self.total_tx_cycles in-place:
   //   self.total_tx_size  -= evicted_size
   //   self.total_tx_cycles -= evicted_cycles

3. self.total_tx_size  = total_tx_size   // OVERWRITES the decremented value
4. self.total_tx_cycles = total_tx_cycles // OVERWRITES the decremented value
``` [1](#0-0) 

After step 3–4, `self.total_tx_size` equals `(pre-eviction total) + entry.size` instead of the correct `(pre-eviction total) − evicted_size + entry.size`. The inflation is exactly equal to the total serialized size of all transactions evicted during `check_and_record_ancestors`.

The eviction path inside `check_and_record_ancestors` is reachable whenever a new transaction has more than `max_ancestors_count` (default 1,000) ancestors, but the excess is attributable to cell-ref parents that can be evicted: [2](#0-1) 

`update_stat_for_remove_tx` correctly decrements the counters for each evicted entry: [3](#0-2) 

But those decrements are immediately discarded when `add_entry` writes the stale pre-computed locals back.

The inflated `total_tx_size` is then used directly in the pool size limit check: [4](#0-3) 

and in the admission check for the next incoming transaction: [5](#0-4) 

---

### Impact Explanation

After a single eviction-triggering submission, `total_tx_size` is permanently inflated by the aggregate size of all evicted transactions. Subsequent calls to `limit_size` will evict additional legitimate transactions to bring the (falsely elevated) counter below `max_tx_pool_size`, and `updated_stat_for_add_tx` will reject new transactions as "pool full" even though the real occupied size is well below the configured limit. The pool's effective capacity is silently reduced proportionally to the inflation, degrading transaction throughput for all users of the node.

---

### Likelihood Explanation

The eviction path in `check_and_record_ancestors` is explicitly supported and tested (see `TxPoolLimitAncestorCount` integration test). An unprivileged submitter needs only to:
1. Submit ~1,000+ independent transactions that all reference the same unspent cell as a `cell_dep` (cell-ref parents).
2. Submit one transaction that *spends* that cell, triggering eviction of the cell-ref parents to satisfy the ancestor count limit. [6](#0-5) 

This is a normal, documented usage pattern. No special privilege, key, or majority hash power is required. The attack is repeatable: each such submission inflates the counter further.

---

### Recommendation

Compute the pre-addition snapshot *after* `check_and_record_ancestors` returns (so evictions are already reflected), or recompute the delta relative to the post-eviction `self.total_tx_size`:

```rust
// Option A: move stat computation after evictions
evicts = self.check_and_record_ancestors(&mut entry)?;
self.record_entry_edges(&entry)?;
let (total_tx_size, total_tx_cycles) =
    self.updated_stat_for_add_tx(entry.size, entry.cycles)?;
self.insert_entry(&entry, status);
self.record_entry_descendants(&entry);
self.track_entry_statics(None, Some(status));
self.total_tx_size = total_tx_size;
self.total_tx_cycles = total_tx_cycles;
```

This ensures the final write reflects the post-eviction baseline rather than the pre-eviction snapshot.

---

### Proof of Concept

1. Configure a node with `max_ancestors_count = 1000` (default) and `max_tx_pool_size = 2_000_000` bytes.
2. Submit a root transaction `tx_root` that creates an output cell.
3. Submit 1,001 independent transactions `tx_ref[0..1000]`, each referencing `tx_root`'s output as a `cell_dep`. Each is ~200 bytes. Pool size ≈ 200,200 bytes; `total_tx_size` = 200,200.
4. Submit `tx_spend` that *spends* `tx_root`'s output. This triggers `check_and_record_ancestors`: ancestors_count = 1,002 > 1,000, so 2 cell-ref parents are evicted (≈ 400 bytes removed). `update_stat_for_remove_tx` sets `self.total_tx_size = 199,800`. Then `add_entry` writes back `total_tx_size = 200,200 + tx_spend.size` — the 400 bytes of evictions are lost.
5. Repeat step 3–4 many times. Each iteration inflates `total_tx_size` by the evicted bytes.
6. Eventually `total_tx_size` exceeds `max_tx_pool_size` even though the real pool occupancy is far below the limit. New legitimate transactions are rejected with `Reject::Full`. [7](#0-6)

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

**File:** tx-pool/src/component/pool_map.rs (L716-721)
```rust
        let total_tx_size = self.total_tx_size.checked_add(tx_size).ok_or_else(|| {
            Reject::Full(format!(
                "tx-pool total_tx_size {} overflows by add {}",
                self.total_tx_size, tx_size
            ))
        })?;
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

**File:** tx-pool/src/pool.rs (L298-298)
```rust
        while self.pool_map.total_tx_size > self.config.max_tx_pool_size {
```

**File:** test/src/specs/tx_pool/limit.rs (L93-116)
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
```

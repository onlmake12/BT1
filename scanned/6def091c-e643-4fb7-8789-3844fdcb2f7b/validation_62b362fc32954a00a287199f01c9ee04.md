### Title
Inflated `total_tx_size`/`total_tx_cycles` Counters Due to Eviction Accounting Race in `add_entry` — (File: `tx-pool/src/component/pool_map.rs`)

---

### Summary

In `PoolMap::add_entry`, the new total size and cycles are computed **before** ancestor-eviction side-effects are applied, then unconditionally written back **after** those evictions have already decremented the counters. This causes `total_tx_size` and `total_tx_cycles` to be permanently inflated by the aggregate size/cycles of all evicted transactions, directly analogous to the StargatePool `balanceOf`-vs-internal-record discrepancy.

---

### Finding Description

`add_entry` in `tx-pool/src/component/pool_map.rs` follows this sequence:

```rust
// Step 1 – snapshot the new totals into LOCAL variables
let (total_tx_size, total_tx_cycles) =
    self.updated_stat_for_add_tx(entry.size, entry.cycles)?;

// Step 2 – may evict N transactions; each eviction calls
//           update_stat_for_remove_tx, which DECREMENTS self.total_tx_size
//           and self.total_tx_cycles
evicts = self.check_and_record_ancestors(&mut entry)?;

// ...insert, link, track...

// Step 3 – OVERWRITES the struct fields with the pre-eviction snapshot,
//           discarding all decrements applied in Step 2
self.total_tx_size = total_tx_size;
self.total_tx_cycles = total_tx_cycles;
``` [1](#0-0) 

`updated_stat_for_add_tx` captures `self.total_tx_size + entry.size` into a local variable before any evictions occur. [2](#0-1) 

`check_and_record_ancestors` calls `remove_entry_and_descendants` when the ancestor count exceeds `max_ancestors_count` and the excess is attributable to cell-ref parents. Each removed entry calls `update_stat_for_remove_tx`, which correctly decrements `self.total_tx_size` and `self.total_tx_cycles`. [3](#0-2) 

`update_stat_for_remove_tx` decrements the live struct fields. [4](#0-3) 

After all evictions, lines 218–219 blindly restore the pre-eviction snapshot, erasing every decrement. The net result is that `total_tx_size` and `total_tx_cycles` are inflated by exactly the sum of the evicted transactions' sizes and cycles. The code even carries a comment acknowledging the inaccuracy: `/// cycles overflow is possible, currently obtaining cycles is not accurate`. [5](#0-4) 

---

### Impact Explanation

`total_tx_size` is the sole counter driving the pool-eviction loop in `TxPool::limit_size`:

```rust
while self.pool_map.total_tx_size > self.config.max_tx_pool_size {
    // evict lowest-fee pending/gap/proposed entries
}
``` [6](#0-5) 

An inflated `total_tx_size` causes `limit_size` to believe the pool is over-capacity and evict additional legitimate transactions that should have been retained. This is a **tx-pool denial-of-service**: honest users' transactions are expelled from the pool even though the real pool occupancy is within limits.

`total_tx_cycles` inflation corrupts the `tx_pool_info` RPC response and the fee-rate estimator inputs, misleading operators and wallets about pool state. [7](#0-6) 

---

### Likelihood Explanation

The eviction path inside `check_and_record_ancestors` is triggered when a submitted transaction has more than `max_ancestors_count` (default 1000) ancestors, and the excess is due to cell-ref parents (transactions that reference the same cell dep). An unprivileged tx-pool submitter can deliberately engineer this:

1. Submit ≥1001 transactions that each carry a `cell_dep` pointing to a specific live cell (e.g., a widely-used lock script output).
2. Submit one transaction that **consumes** that cell as an input.

Step 2 triggers the eviction of the lowest-fee cell-ref parents to bring the ancestor count below the limit. The integration test `TxPoolLimitAncestorCount` demonstrates exactly this scenario with 2000 cell-ref transactions. [8](#0-7) 

The attacker must pay fees for all submitted transactions, but the inflation persists until the node restarts or the pool is cleared, and the damage (eviction of honest transactions) is immediate.

---

### Recommendation

Compute the new totals **after** all evictions have been applied, not before. Replace the pre-eviction snapshot pattern with a post-eviction increment:

```rust
// Remove the pre-eviction snapshot entirely.
// After check_and_record_ancestors returns, self.total_tx_size and
// self.total_tx_cycles already reflect all evictions.
// Simply add the new entry's contribution:
evicts = self.check_and_record_ancestors(&mut entry)?;
self.record_entry_edges(&entry)?;
self.insert_entry(&entry, status);
self.record_entry_descendants(&entry);
self.track_entry_statics(None, Some(status));
// Now safely add the new entry's weight:
self.total_tx_size = self.total_tx_size.saturating_add(entry.size);
self.total_tx_cycles = self.total_tx_cycles.saturating_add(entry.cycles);
```

Alternatively, keep the overflow-check semantics of `updated_stat_for_add_tx` but call it **after** `check_and_record_ancestors` returns, so it operates on the post-eviction baseline.

---

### Proof of Concept

**Setup:** pool with `max_ancestors_count = 1000`, `max_tx_pool_size = 180_000_000`.

1. Submit 1001 transactions `T1…T1001`, each with `cell_dep` pointing to cell `C`. Each is ~200 bytes. `total_tx_size ≈ 200_200`.
2. Submit transaction `T_consume` that spends cell `C` as an input. This triggers `check_and_record_ancestors`:
   - `ancestors_count = 1002 > 1000`
   - `cell_ref_parents.len() = 1001`, so `1002 - 1001 = 1 ≤ 1000` → eviction path taken
   - 2 lowest-fee cell-ref parents are evicted (to bring count to 1000). Each eviction calls `update_stat_for_remove_tx`, decrementing `self.total_tx_size` by ~400 bytes total.
   - After evictions: `self.total_tx_size ≈ 199_800`.
3. Lines 218–219 restore the pre-eviction snapshot: `self.total_tx_size = 200_200 + size(T_consume) ≈ 200_400`.
4. **Inflation = ~400 bytes** (the evicted transactions' sizes are double-counted).

Repeating this attack with larger batches inflates `total_tx_size` by the sum of all evicted transaction sizes per invocation. Once `total_tx_size` exceeds `max_tx_pool_size` due to accumulated inflation, `limit_size` begins evicting honest transactions on every subsequent submission.

### Citations

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

**File:** tx-pool/src/service.rs (L1089-1090)
```rust
            total_tx_size: tx_pool.pool_map.total_tx_size,
            total_tx_cycles: tx_pool.pool_map.total_tx_cycles,
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

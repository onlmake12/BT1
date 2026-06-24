Audit Report

## Title
Stale Pre-Computed `total_tx_size`/`total_tx_cycles` Overwrites Correct Post-Eviction Values in `PoolMap::add_entry` — (File: `tx-pool/src/component/pool_map.rs`)

## Summary
In `PoolMap::add_entry`, aggregate pool-size counters are pre-computed into locals before a conditional eviction step. When `check_and_record_ancestors` evicts entries via `remove_entry_and_descendants` → `remove_entry` → `update_stat_for_remove_tx`, it correctly decrements `self.total_tx_size`/`self.total_tx_cycles` in place. Lines 218–219 then unconditionally overwrite those fields with the stale pre-eviction locals, permanently inflating both counters by the size and cycles of every evicted entry. An attacker can repeat this to drive `total_tx_size` far above the true pool size, causing the node to spuriously evict and reject legitimate transactions.

## Finding Description
`PoolMap` tracks aggregate counters `total_tx_size` and `total_tx_cycles` at lines 69–71. [1](#0-0) 

`add_entry` (lines 200–221) executes this sequence:

**Step 1 — Pre-compute (lines 210–211):** `updated_stat_for_add_tx` reads the current `self.total_tx_size`/`self.total_tx_cycles`, adds `entry.size`/`entry.cycles`, and stores the result in **local** variables `total_tx_size`/`total_tx_cycles`. `self.*` fields are not yet modified. [2](#0-1) 

**Step 2 — Conditional eviction (line 213):** `check_and_record_ancestors` is called. When `ancestors_count > max_ancestors_count` and `ancestors_count - cell_ref_parents.len() <= max_ancestors_count` (line 603), it calls `remove_entry_and_descendants` (line 618), which calls `remove_entry` (line 263), which calls `update_stat_for_remove_tx` (line 247). This **mutates** `self.total_tx_size` and `self.total_tx_cycles` in place, decrementing them by the evicted entries' sizes and cycles. [3](#0-2) [4](#0-3) 

**Step 3 — Unconditional overwrite (lines 218–219):** The stale pre-eviction locals are written back unconditionally, discarding the decrements from step 2. [5](#0-4) 

Concretely, if the pool is at size `T` and an entry of size `S_evict` is removed while a new entry of size `S_new` is added:
- **Correct:** `T − S_evict + S_new`
- **Actual:** `T + S_new` (inflated by `S_evict`)

`update_stat_for_remove_tx` correctly decrements `self.total_tx_size` at lines 738–740, but this decrement is immediately discarded by the overwrite at line 218. [6](#0-5) 

## Impact Explanation
`total_tx_size` is the sole guard for pool-size enforcement in `limit_size` (line 298 of `pool.rs`), which evicts pending/proposed transactions until the counter drops below `max_tx_pool_size`. [7](#0-6) 

It also gates the overflow check in `updated_stat_for_add_tx` (lines 716–721) that rejects incoming transactions with `Reject::Full`. [8](#0-7) 

When inflated, the node will: (1) spuriously evict legitimate pending/proposed transactions via `limit_size`, and (2) spuriously reject new honest transactions with `Reject::Full`. An attacker can repeat the trigger to drive `total_tx_size` arbitrarily above the true pool size, making the node permanently unable to accept or retain legitimate transactions.

**Impact: High — Vulnerabilities or bad designs which could cause CKB network congestion with few costs.** A targeted node becomes unable to propagate honest transactions, degrading network throughput.

## Likelihood Explanation
The eviction path requires: (1) a chain of ≥ `max_ancestors_count` (default 25) transactions in the pool, (2) at least one of those transactions uses an output as a cell dep that the new transaction spends as an input (`cell_ref_parents` non-empty), and (3) removing those `cell_ref_parents` brings the ancestor count within the limit. An unprivileged attacker can deliberately construct this scenario by submitting a 25-deep chain where one transaction uses a specific UTXO as a cell dep, then submitting a transaction spending that UTXO. Each such submission inflates the counters by the evicted entries' sizes. The setup is repeatable with low cost (only transaction fees), and each iteration permanently inflates the counters.

## Recommendation
Remove the pre-computation of `total_tx_size`/`total_tx_cycles` before `check_and_record_ancestors`. After all mutations (`insert_entry`, `record_entry_descendants`, etc.) complete, apply the add increment directly on top of the already-decremented `self.total_tx_size`/`self.total_tx_cycles`:

```rust
// After all evictions and insertions are complete:
self.total_tx_size = self.total_tx_size
    .checked_add(entry.size)
    .ok_or_else(|| Reject::Full(format!(
        "tx-pool total_tx_size {} overflows by add {}",
        self.total_tx_size, entry.size
    )))?;
self.total_tx_cycles = self.total_tx_cycles
    .checked_add(entry.cycles)
    .ok_or_else(|| Reject::Full(format!(
        "tx-pool total_tx_cycles {} overflows by add {}",
        self.total_tx_cycles, entry.cycles
    )))?;
```

This ensures the add increment is applied on top of the post-eviction state rather than the pre-eviction state, making add and remove operations complementary and consistent. [9](#0-8) 

## Proof of Concept
1. Start with an empty pool; `max_tx_pool_size = 10_000`, `max_ancestors_count = 25`.
2. Submit a chain of 25 transactions `T1 → T2 → … → T25`, where `T10` uses on-chain output `O` as a cell dep. Each tx is ~100 bytes. `total_tx_size = 2500`.
3. Submit transaction `N` (size=100) that spends output `O` as an input. `N` has `T1…T25` as ancestors (`ancestors_count = 26 > 25`). `T10` is a `cell_ref_parent`; `26 − 1 = 25 ≤ 25` satisfies the eviction condition at line 603.
4. Inside `add_entry` for `N`:
   - `updated_stat_for_add_tx(100, ...)` → local `total_tx_size = 2600`.
   - `check_and_record_ancestors` evicts `T10` and its descendants via `remove_entry_and_descendants` → `update_stat_for_remove_tx` → `self.total_tx_size` decremented.
   - Line 218: `self.total_tx_size = 2600` (stale). **Inflation: +size of evicted entries.**
5. Pool actually holds fewer entries than `total_tx_size` reflects.
6. Repeat steps 2–4. After sufficient iterations, `total_tx_size > 10_000` while the real pool holds far fewer bytes. `limit_size` begins evicting legitimate transactions and `updated_stat_for_add_tx` returns `Reject::Full` for all new submissions. [4](#0-3)

### Citations

**File:** tx-pool/src/component/pool_map.rs (L68-71)
```rust
    // sum of all tx_pool tx's virtual sizes.
    pub(crate) total_tx_size: usize,
    // sum of all tx_pool tx's cycles.
    pub(crate) total_tx_cycles: Cycle,
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

**File:** tx-pool/src/component/pool_map.rs (L246-247)
```rust
            self.track_entry_statics(Some(entry.status), None);
            self.update_stat_for_remove_tx(entry.inner.size, entry.inner.cycles);
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

**File:** tx-pool/src/component/pool_map.rs (L733-741)
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
```

**File:** tx-pool/src/pool.rs (L298-298)
```rust
        while self.pool_map.total_tx_size > self.config.max_tx_pool_size {
```

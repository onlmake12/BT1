Audit Report

## Title
Stale Pre-Eviction Snapshot Permanently Inflates `total_tx_size` in `PoolMap::add_entry` — (File: `tx-pool/src/component/pool_map.rs`)

## Summary
In `PoolMap::add_entry`, `updated_stat_for_add_tx` captures `self.total_tx_size + entry.size` into a local variable **before** `check_and_record_ancestors` runs. When that function evicts cell-dep parent transactions via `remove_entry_and_descendants` → `remove_entry` → `update_stat_for_remove_tx`, the live `self.total_tx_size` is correctly decremented. However, the stale pre-eviction snapshot is then unconditionally written back to `self.total_tx_size`, erasing the decrements and permanently inflating the counter by the aggregate size of all evicted transactions. This causes `limit_size` to evict valid transactions and reject new submissions with `Reject::Full` even when actual pool occupancy is well below the configured limit.

## Finding Description
`add_entry` (lines 200–221) executes in this order:

1. **Lines 210–211**: `updated_stat_for_add_tx(entry.size, entry.cycles)` reads `self.total_tx_size`, adds the new entry's size, and stores the result in a local variable. No evictions have occurred yet. [1](#0-0) 

2. **Line 213**: `check_and_record_ancestors(&mut entry)` is called. When `ancestors_count > max_ancestors_count` but `ancestors_count.saturating_sub(cell_ref_parents.len()) <= max_ancestors_count` (line 603), the eviction branch fires (lines 615–625), calling `remove_entry_and_descendants` for each cell-dep parent. Each call chains to `remove_entry` (line 263) → `update_stat_for_remove_tx` (line 247), which correctly decrements the **live** `self.total_tx_size`. [2](#0-1) [3](#0-2) [4](#0-3) 

3. **Lines 218–219**: The stale local variables are written back unconditionally, overwriting the correctly-decremented live counters with the pre-eviction snapshot: [5](#0-4) 

`updated_stat_for_add_tx` itself is confirmed to read `self.total_tx_size` at call time and return a plain arithmetic result — it has no side effects and does not re-read the field after evictions: [6](#0-5) 

`update_stat_for_remove_tx` correctly decrements `self.total_tx_size` in place, but those decrements are erased by the write-back at lines 218–219: [7](#0-6) 

The `recompute_total_stat` self-healing path is only triggered on **underflow** (checked_sub failure), never on overflow, so the inflation is never corrected: [8](#0-7) 

## Impact Explanation
`limit_size` uses `self.pool_map.total_tx_size > self.config.max_tx_pool_size` as its sole eviction condition: [9](#0-8) 

An inflated `total_tx_size` causes `limit_size` to believe the pool is over capacity, evicting otherwise-valid pending transactions and rejecting new valid submissions with `Reject::Full`. The inflation accumulates monotonically across repeated attack iterations. This matches the **High (10001–15000 points)** impact class: *"Vulnerabilities or bad designs which could cause CKB network congestion with few costs."*

## Likelihood Explanation
The trigger condition — a new transaction whose ancestor count exceeds `max_ancestors_count` due to cell-dep references, but falls within limits after evicting those cell-dep parents — is reachable by any unprivileged caller of the `send_transaction` RPC or any P2P relay peer. No key material, privileged access, or majority hash power is required. The attack is repeatable and the inflation accumulates with each iteration.

## Recommendation
Move the `updated_stat_for_add_tx` call to **after** `check_and_record_ancestors` completes, so evictions are already reflected in `self.total_tx_size` before the snapshot is taken:

```diff
- let (total_tx_size, total_tx_cycles) =
-     self.updated_stat_for_add_tx(entry.size, entry.cycles)?;
  trace!("pool_map.add_{:?} {}", status, entry.transaction().hash());
  evicts = self.check_and_record_ancestors(&mut entry)?;
  self.record_entry_edges(&entry)?;
  self.insert_entry(&entry, status);
  self.record_entry_descendants(&entry);
  self.track_entry_statics(None, Some(status));
+ let (total_tx_size, total_tx_cycles) =
+     self.updated_stat_for_add_tx(entry.size, entry.cycles)?;
  self.total_tx_size = total_tx_size;
  self.total_tx_cycles = total_tx_cycles;
  Ok((true, evicts))
```

## Proof of Concept
1. Configure a node with `max_ancestors_count = N` and a finite `max_tx_pool_size`.
2. Submit transactions `T1…TN` where each `Ti` is referenced as a cell dep by `T_{i+1}`. All are accepted; `total_tx_size` reflects their aggregate size `S`.
3. Submit `Tnew` referencing `T1` as a cell dep. Its ancestor count is `N+1 > max_ancestors_count`, but `cell_ref_parents = {T1}` so `(N+1) - 1 = N <= max_ancestors_count`. The eviction branch fires: `T1` and its descendants are removed, decrementing `self.total_tx_size` by `D`. Then lines 218–219 write back the pre-eviction snapshot `S + Tnew.size`, inflating the counter by `D`.
4. Observe via RPC or logs that `total_tx_size` reports `S + Tnew.size` instead of the correct `S - D + Tnew.size`.
5. Repeat steps 2–4. After `ceil((max_tx_pool_size - S) / D)` iterations, `total_tx_size > max_tx_pool_size` even though the actual pool is nearly empty. `limit_size` begins evicting all remaining transactions and new submissions are rejected with `Reject::Full`.

### Citations

**File:** tx-pool/src/component/pool_map.rs (L210-211)
```rust
        let (total_tx_size, total_tx_cycles) =
            self.updated_stat_for_add_tx(entry.size, entry.cycles)?;
```

**File:** tx-pool/src/component/pool_map.rs (L213-213)
```rust
        evicts = self.check_and_record_ancestors(&mut entry)?;
```

**File:** tx-pool/src/component/pool_map.rs (L218-219)
```rust
        self.total_tx_size = total_tx_size;
        self.total_tx_cycles = total_tx_cycles;
```

**File:** tx-pool/src/component/pool_map.rs (L247-247)
```rust
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

**File:** tx-pool/src/component/pool_map.rs (L742-755)
```rust
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
```

**File:** tx-pool/src/pool.rs (L298-298)
```rust
        while self.pool_map.total_tx_size > self.config.max_tx_pool_size {
```

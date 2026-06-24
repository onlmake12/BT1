Audit Report

## Title
Stale Pre-Eviction Stat Snapshot Overwrites Decremented Counters in `add_entry` — (File: tx-pool/src/component/pool_map.rs)

## Summary
In `PoolMap::add_entry`, `updated_stat_for_add_tx` snapshots `self.total_tx_size + entry.size` into local variables before `check_and_record_ancestors` runs. When that call evicts cell-dep-referencing ancestors via `remove_entry_and_descendants` → `remove_entry` → `update_stat_for_remove_tx`, the decrements are applied directly to `self.total_tx_size`/`self.total_tx_cycles`. The stale pre-eviction snapshot is then unconditionally written back at lines 218–219, permanently discarding every decrement. Repeated triggering monotonically inflates the counters, causing `limit_size` to evict legitimate transactions and `updated_stat_for_add_tx` to reject honest submissions with `Reject::Full` even when the pool has real capacity.

## Finding Description
**Root cause — `add_entry` (lines 200–221):**

The snapshot is taken at lines 210–211 before any evictions occur: [1](#0-0) 

`check_and_record_ancestors` at line 213 may call `remove_entry_and_descendants` (line 618 inside the function), which calls `remove_entry`, which calls `update_stat_for_remove_tx` at line 247, decrementing `self.total_tx_size` and `self.total_tx_cycles` in place: [2](#0-1) [3](#0-2) 

After evictions complete, lines 218–219 unconditionally overwrite the live, eviction-adjusted counters with the stale pre-eviction snapshot: [4](#0-3) 

**Eviction path — `check_and_record_ancestors` (lines 603–625):**
When `ancestors_count > max_ancestors_count` but `ancestors_count - cell_ref_parents.len() <= max_ancestors_count`, the lowest-fee cell-dep-referencing parents are removed: [5](#0-4) 

**Why existing checks fail:**
`updated_stat_for_add_tx` only checks for integer overflow on the pre-eviction baseline; it does not account for the fact that evictions will later reduce the true pool size, and there is no guard preventing the stale snapshot from overwriting the live counters: [6](#0-5) 

## Impact Explanation
**Pool-size enforcement is broken:** `limit_size` loops while `self.pool_map.total_tx_size > self.config.max_tx_pool_size`. With an inflated counter, this loop fires when the pool has real capacity, evicting legitimate pending transactions: [7](#0-6) 

**New-transaction admission is broken:** `updated_stat_for_add_tx` rejects any incoming transaction whose addition would overflow the inflated baseline, causing honest transactions to be rejected with `Reject::Full` even when the pool is not actually full.

**RPC reporting is incorrect:** `tx_pool_info` reads `total_tx_size` and `total_tx_cycles` directly, so monitoring tools, wallets, and fee estimators receive inflated values: [8](#0-7) 

This matches the **High** impact: *"Vulnerabilities or bad designs which could cause CKB network congestion with few costs."* An attacker can repeatedly submit cheap crafted transactions to compound the inflation monotonically, progressively degrading mempool admission for all honest users across the network.

## Likelihood Explanation
The trigger requires no privileged access. Any unprivileged user can submit a linear input chain T1 → … → T24, then submit T_dep referencing T1's output as a cell dep (a standard CKB pattern), then submit T_new spending T24's output and listing T_dep as a cell dep. This gives `ancestors_count = 26 > 25`, with `cell_ref_parents = {T_dep}`, so `26 - 1 = 25 ≤ 25` triggers the eviction path. Each iteration inflates `total_tx_size` by `T_dep.size` plus any descendants. The attack is cheap, repeatable, compounds monotonically, and requires no majority hashpower, no leaked keys, and no victim mistakes.

## Recommendation
Move the stat computation to **after** all evictions have completed, so it reads the already-decremented baseline:

```rust
evicts = self.check_and_record_ancestors(&mut entry)?;
// Compute correct final totals against the post-eviction baseline
let (total_tx_size, total_tx_cycles) =
    self.updated_stat_for_add_tx(entry.size, entry.cycles)?;
self.record_entry_edges(&entry)?;
self.insert_entry(&entry, status);
self.record_entry_descendants(&entry);
self.track_entry_statics(None, Some(status));
self.total_tx_size = total_tx_size;
self.total_tx_cycles = total_tx_cycles;
```

## Proof of Concept
1. Configure a node with default `max_ancestors_count = 25`.
2. Submit T1 → T2 → … → T24 as a linear input chain. All 24 are accepted into the pool.
3. Submit T_dep: takes T1's output cell as a **cell dep** (not input). T_dep is now a `cell_ref_parent` of any future transaction descending from T1.
4. Submit T_new: spends T24's output (25 input-chain ancestors including itself) and lists T_dep's output as a cell dep.
   - `ancestors_count = 26`, `cell_ref_parents = {T_dep}`, `26 - 1 = 25 ≤ 25` → eviction path taken.
   - `remove_entry_and_descendants(T_dep)` → `remove_entry` → `update_stat_for_remove_tx` decrements `self.total_tx_size`.
   - `add_entry` line 218 overwrites `self.total_tx_size` with the pre-eviction snapshot, erasing the decrement.
5. Query `tx_pool_info` via RPC: `total_tx_size` is inflated by `T_dep.size`.
6. Repeat steps 2–5 N times: `total_tx_size` grows by `N × T_dep.size` while the actual pool shrinks.
7. Once `total_tx_size` exceeds `max_tx_pool_size`, `limit_size` begins evicting legitimate transactions and new honest submissions are rejected with `Reject::Full`.

### Citations

**File:** tx-pool/src/component/pool_map.rs (L210-211)
```rust
        let (total_tx_size, total_tx_cycles) =
            self.updated_stat_for_add_tx(entry.size, entry.cycles)?;
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

**File:** tx-pool/src/pool.rs (L298-298)
```rust
        while self.pool_map.total_tx_size > self.config.max_tx_pool_size {
```

**File:** tx-pool/src/service.rs (L1089-1090)
```rust
            total_tx_size: tx_pool.pool_map.total_tx_size,
            total_tx_cycles: tx_pool.pool_map.total_tx_cycles,
```

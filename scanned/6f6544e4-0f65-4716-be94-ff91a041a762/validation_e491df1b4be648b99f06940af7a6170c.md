Audit Report

## Title
`total_tx_size` / `total_tx_cycles` Inflated by Stale Snapshot Overwrite When Evictions Occur in `add_entry` — (`tx-pool/src/component/pool_map.rs`)

## Summary

In `PoolMap::add_entry`, the new pool size and cycle totals are captured into local variables before `check_and_record_ancestors` runs. If that function evicts transactions (via `remove_entry_and_descendants`), those removals correctly decrement `self.total_tx_size` / `self.total_tx_cycles` in-place. However, the final assignment at the end of `add_entry` unconditionally overwrites those decremented values with the stale pre-eviction snapshot, permanently inflating the pool's accounting. The inflation persists and compounds with each subsequent eviction-triggering submission, enabling an unprivileged attacker to cause unnecessary eviction of legitimate transactions and premature rejection of valid submissions.

## Finding Description

In `add_entry` (L200–221), the sequence is:

1. **Line 210–211**: `updated_stat_for_add_tx` computes `self.total_tx_size + entry.size` and stores it in a local variable `total_tx_size`. [1](#0-0) 

2. **Line 213**: `check_and_record_ancestors` is called. When `ancestors_count > max_ancestors_count` but reducible via `cell_ref_parents`, it calls `remove_entry_and_descendants` in a loop (L618), which internally calls `update_stat_for_remove_tx`, decrementing `self.total_tx_size` and `self.total_tx_cycles` in-place. [2](#0-1) [3](#0-2) 

3. **Lines 218–219**: The stale local variables (computed before any evictions) overwrite the correctly-decremented `self.total_tx_size` and `self.total_tx_cycles`. [4](#0-3) 

The net result is:
```
final total_tx_size = original_total + entry.size
                    ≠ original_total − evicted_sizes + entry.size   (correct)
```

The pool's size counter is inflated by exactly the sum of the sizes of all evicted transactions per triggering submission. No existing guard prevents this: `updated_stat_for_add_tx` only checks for overflow, not for post-eviction correctness, and `update_stat_for_remove_tx`'s in-place updates are simply discarded by the overwrite.

## Impact Explanation

`total_tx_size` is the authoritative counter driving two critical behaviors:

- **`limit_size`** (L298): evicts transactions while `total_tx_size > max_tx_pool_size`. An inflated counter causes legitimate, already-accepted transactions to be unnecessarily evicted from the pool. [5](#0-4) 

- **`updated_stat_for_add_tx`** (L716–721): rejects new submissions with `Reject::Full` when `total_tx_size` would overflow. An inflated counter causes premature rejection of valid transactions even when the pool has real capacity. [6](#0-5) 

The inflation is permanent (until node restart) and compounds with each triggering submission. This maps to **High (10001–15000 points): Vulnerabilities or bad designs which could cause CKB network congestion with few costs** — an attacker can repeatedly inflate the counter to force eviction of honest transactions and block new valid submissions, degrading mempool utility across the network.

## Likelihood Explanation

The eviction path in `check_and_record_ancestors` is reachable by any unprivileged user via the `send_transaction` RPC. Required conditions:

- New transaction's ancestor count exceeds `max_ancestors_count` (default 25).
- Some ancestors are "cell-ref parents" (share a cell dep with the new transaction's ancestors), so `ancestors_count − cell_ref_parents.len() ≤ max_ancestors_count` triggers the eviction branch rather than an outright rejection. [7](#0-6) 

An attacker can deliberately craft this: submit a chain of 26+ transactions all referencing a shared cell dep `D`, then submit a 27th spending the chain tip and also referencing `D`. This is repeatable with low cost (only transaction fees), requires no privileged access, key material, or majority hashpower, and each iteration inflates the counter by the evicted transactions' sizes.

## Recommendation

Move `updated_stat_for_add_tx` to execute **after** `check_and_record_ancestors` completes, so it reads the already-updated (post-eviction) `self.total_tx_size` and `self.total_tx_cycles`:

```rust
evicts = self.check_and_record_ancestors(&mut entry)?;
self.record_entry_edges(&entry)?;
self.insert_entry(&entry, status);
self.record_entry_descendants(&entry);
self.track_entry_statics(None, Some(status));

// Recompute AFTER evictions have already updated self.total_tx_size / total_tx_cycles
let (total_tx_size, total_tx_cycles) =
    self.updated_stat_for_add_tx(entry.size, entry.cycles)?;
self.total_tx_size = total_tx_size;
self.total_tx_cycles = total_tx_cycles;
```

Alternatively, separate the overflow pre-flight check (using a non-capturing guard) from the final assignment, and always derive final totals from the post-eviction state.

## Proof of Concept

1. Start node with empty pool (`total_tx_size = 0`, `max_ancestors_count = 25`).
2. Submit tx₁ → tx₂ → … → tx₂₆, each referencing shared cell dep `D`, each 200 bytes. Pool holds 26 entries; `total_tx_size = 5200`.
3. Submit tx₂₇ spending tx₂₆'s output and referencing `D`. `ancestors_count = 27 > 25`; `cell_ref_parents = {tx₁…tx₂₆}`; `27 − 26 = 1 ≤ 25` → eviction branch triggers.
4. `updated_stat_for_add_tx` captures `total_tx_size_local = 5200 + 200 = 5400`.
5. `check_and_record_ancestors` evicts tx₁ (200 bytes): `self.total_tx_size` decremented to `5000`.
6. tx₂₇ inserted. Lines 218–219 assign `self.total_tx_size = 5400` (overwrites `5000`).
7. Pool holds 26 entries (tx₂…tx₂₇) = 5200 actual bytes, but `total_tx_size = 5400` — inflated by 200 bytes.
8. Repeating this pattern accumulates inflation. Once `total_tx_size` exceeds `max_tx_pool_size`, `limit_size` begins evicting honest transactions; `updated_stat_for_add_tx` begins rejecting valid submissions with `Reject::Full`.

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

**File:** tx-pool/src/component/pool_map.rs (L603-605)
```rust
        if ancestors_count.saturating_sub(cell_ref_parents.len()) <= self.max_ancestors_count {
            // if ancestors count exceed limitation,
            // try to evict some conflicted transactions due to ref cells
```

**File:** tx-pool/src/component/pool_map.rs (L613-625)
```rust
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

**File:** tx-pool/src/pool.rs (L297-299)
```rust
        let mut ret = None;
        while self.pool_map.total_tx_size > self.config.max_tx_pool_size {
            let next_evict_entry = || {
```

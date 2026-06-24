Audit Report

## Title
`total_tx_size` Permanently Inflated by Stale Local Variable Overwrite After Cell-Ref-Parent Eviction — (File: `tx-pool/src/component/pool_map.rs`)

## Summary

In `add_entry`, `updated_stat_for_add_tx` captures a snapshot of `self.total_tx_size + tx_size` into a local variable before `check_and_record_ancestors` runs. When that function evicts `cell_ref_parents`, each eviction calls `update_stat_for_remove_tx`, which correctly decrements `self.total_tx_size`. Line 218 then unconditionally overwrites `self.total_tx_size` with the stale pre-eviction local, erasing all decrements. The result is a permanently inflated `total_tx_size` that causes `limit_size` to prematurely evict legitimate transactions.

## Finding Description

`add_entry` (lines 200–221) executes in this order:

1. **Lines 210–211**: `updated_stat_for_add_tx` reads `self.total_tx_size`, adds `entry.size`, and returns the result as a local variable. It does not write to `self`.
2. **Line 213**: `check_and_record_ancestors` is called. When `ancestors_count > max_ancestors_count` but `ancestors_count - cell_ref_parents.len() <= max_ancestors_count` (lines 603–625), it loops calling `remove_entry_and_descendants`, which internally calls `update_stat_for_remove_tx`. That function writes `self.total_tx_size -= evicted_tx.size` directly (line 739). After the loop, `self.total_tx_size` correctly equals `old_total − Σ(evicted_sizes)`.
3. **Line 218**: `self.total_tx_size = total_tx_size` unconditionally overwrites the correctly decremented field with `old_total + new_tx.size`, inflating by exactly `Σ(evicted_sizes)`.

The only recovery path, `recompute_total_stat`, is only triggered on underflow (lines 742–755), never on inflation. The error is permanent and accumulates across submissions.

`limit_size` (pool.rs lines 298–328) loops while `self.pool_map.total_tx_size > self.config.max_tx_pool_size` and evicts the lowest-fee-rate pending transactions. It is called unconditionally after every successful `_submit_entry` (process.rs lines 150–152). With an inflated counter, it evicts legitimate transactions that would not have been evicted under the true pool size.

## Impact Explanation

An attacker can repeatedly submit transactions that trigger the cell-ref-parent eviction branch, each time inflating `total_tx_size` by the sum of evicted transaction sizes. Because the inflation accumulates and is never corrected downward, the attacker can drive `total_tx_size` arbitrarily above the real pool size, causing `limit_size` to continuously evict honest transactions. This matches: **High (10001–15000 points) — Vulnerabilities or bad designs which could cause CKB network congestion with few costs.**

## Likelihood Explanation

The trigger requires a transaction whose ancestor count exceeds `max_ancestors_count` (default 25) but where the excess ancestors are all `cell_ref_parents`. An attacker can construct this deliberately using only valid transactions and standard fees, with no privileged access. The path is reachable via standard P2P relay (`submit_remote_tx`) or RPC (`send_transaction`). The condition is repeatable: each triggering submission inflates the counter by the sum of evicted sizes, and the inflation is never corrected.

## Recommendation

Move the stat update to after `check_and_record_ancestors` completes, so it reads the post-eviction `self.total_tx_size`:

```rust
evicts = self.check_and_record_ancestors(&mut entry)?;
self.record_entry_edges(&entry)?;
self.insert_entry(&entry, status);
self.record_entry_descendants(&entry);
self.track_entry_statics(None, Some(status));
// Compute totals AFTER eviction
let (total_tx_size, total_tx_cycles) =
    self.updated_stat_for_add_tx(entry.size, entry.cycles)?;
self.total_tx_size = total_tx_size;
self.total_tx_cycles = total_tx_cycles;
```

Alternatively, subtract the sizes of all entries in `evicts` from the pre-computed local variable before assigning, or call `recompute_total_stat` whenever `evicts` is non-empty.

## Proof of Concept

```
Setup:
  max_ancestors_count = 25
  max_tx_pool_size    = 1_000_000 bytes
  Pool contains tx0..tx23 (24 txs, each 1000 bytes), total_tx_size = 24_000
  tx0 is also used as a cell dep by tx_extra

Submit tx_extra (inputs tx23's output, cell_dep = tx0's output):
  ancestors = {tx0..tx23} → ancestors_count = 25 (= max, no eviction yet)

Submit tx_final with tx0..tx24 as ancestors (26 total), tx0 as cell_ref_parent:
  ancestors_count = 26 > 25
  26 - 1 = 25 ≤ 25 → eviction branch entered

  updated_stat_for_add_tx: local_total = 24_000 + 1000 = 25_000
  check_and_record_ancestors evicts tx0:
    update_stat_for_remove_tx: self.total_tx_size = 24_000 - 1000 = 23_000
  Line 218: self.total_tx_size = 25_000  ← BUG (should be 24_000)

Repeat N times → total_tx_size inflated by N × 1000
→ limit_size fires and evicts honest transactions even though real pool is within bounds
```

A unit test can be written against `PoolMap` directly: populate the pool with the described chain, call `add_entry` for the triggering transaction, then assert `pool_map.total_tx_size == pool_map.entries.iter().map(|e| e.size).sum()`. The assertion will fail, confirming the inflation.
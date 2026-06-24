Audit Report

## Title
`total_tx_size` Inflation via Stale Local Variable Overwrite After Cell-Ref-Parent Eviction — (File: `tx-pool/src/component/pool_map.rs`)

## Summary
In `add_entry`, `updated_stat_for_add_tx` computes new pool totals into local variables before `check_and_record_ancestors` runs. When that function evicts `cell_ref_parents`, each eviction calls `update_stat_for_remove_tx`, which correctly decrements `self.total_tx_size`. However, at the end of `add_entry`, the stale local variable — computed before any eviction — unconditionally overwrites `self.total_tx_size`, erasing all decrements. The result is a permanently inflated `total_tx_size` that causes `limit_size` to prematurely evict legitimate transactions from the pool.

## Finding Description
The exact sequence in `add_entry` (lines 210–219):

```
Line 210-211: (total_tx_size, total_tx_cycles) = self.updated_stat_for_add_tx(...)
              → local_total = self.total_tx_size + new_tx.size  (snapshot taken here)
Line 213:     evicts = self.check_and_record_ancestors(&mut entry)?
              → may call remove_entry_and_descendants
              → remove_entry → update_stat_for_remove_tx
              → self.total_tx_size -= evicted_tx.size  (correct decrement to self)
Line 218:     self.total_tx_size = total_tx_size  (OVERWRITES the decrement with stale local)
```

`updated_stat_for_add_tx` (lines 711–729) is a read-only method: it reads `self.total_tx_size`, adds the new tx size, and returns the result without writing to `self`. `update_stat_for_remove_tx` (lines 733–758) does write to `self.total_tx_size` via `checked_sub`. The eviction branch in `check_and_record_ancestors` (lines 603–625) is entered when `ancestors_count.saturating_sub(cell_ref_parents.len()) <= self.max_ancestors_count` but `ancestors_count > self.max_ancestors_count`, calling `remove_entry_and_descendants` in a loop. After the loop, `self.total_tx_size` correctly equals `old_total − Σ(evicted sizes)`. Line 218 then overwrites it with `old_total + new_tx.size`, inflating it by exactly `Σ(evicted sizes)`. The only recovery path, `recompute_total_stat` (lines 698–708), is only triggered on underflow, never on inflation, so the error is permanent and accumulates.

## Impact Explanation
`limit_size` (pool.rs lines 298–328) loops while `self.pool_map.total_tx_size > self.config.max_tx_pool_size`, evicting the lowest-fee-rate pending transactions. It is called immediately after every successful `_submit_entry` (process.rs lines 150–152). With an inflated `total_tx_size`, `limit_size` evicts legitimate transactions that would not have been evicted under the true pool size. Because the inflation accumulates across submissions, an attacker can repeatedly trigger the condition to drive `total_tx_size` arbitrarily above the real value, causing continuous premature eviction of honest transactions and degrading pool throughput. This matches the allowed impact: **"Vulnerabilities or bad designs which could cause CKB network congestion with few costs" (High, 10001–15000 points)**, since honest transactions are continuously expelled from the mempool, preventing their inclusion in blocks.

## Likelihood Explanation
The trigger requires a transaction whose ancestor count exceeds `max_ancestors_count` (default 25) but where the excess ancestors are all `cell_ref_parents`. An attacker can construct this deliberately with only valid transaction fees and no privileged access: submit a chain of 25+ transactions where some intermediate transactions are also used as cell deps, then submit a final transaction referencing those cell-dep ancestors. The path is reachable via standard P2P relay (`submit_remote_tx`) or RPC (`send_transaction`). The condition is repeatable, allowing the inflation to accumulate indefinitely.

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

Alternatively, subtract the sizes of all evicted transactions from the pre-computed local variable before assigning, or call `recompute_total_stat` whenever evictions occur.

## Proof of Concept
```
Setup:
  max_ancestors_count = 25
  max_tx_pool_size    = 1_000_000 bytes
  Pool: tx0..tx23 in a chain (24 txs, each 1000 bytes)
  tx0 is also used as a cell dep by tx_new
  total_tx_size before = 24_000

Submit tx_extra (ancestors = tx0..tx24, ancestors_count = 26 > 25,
                 cell_ref_parents = {tx0}, 26 - 1 = 25 ≤ 25 → eviction branch):

  updated_stat_for_add_tx: local_total = 24_000 + 1000 = 25_000
  check_and_record_ancestors evicts tx0:
    update_stat_for_remove_tx: self.total_tx_size = 24_000 - 1000 = 23_000
  Line 218: self.total_tx_size = 25_000  ← BUG (should be 23_000 + 1000 = 24_000)
  Inflation = 1000 bytes per trigger

Repeat N times → total_tx_size inflated by N × evicted_size
→ limit_size fires and evicts honest transactions even though real pool is within bounds
```

A unit test can confirm this by asserting `pool_map.total_tx_size` equals the sum of sizes of all entries after the trigger sequence, using `recompute_total_stat` as the ground truth comparator.
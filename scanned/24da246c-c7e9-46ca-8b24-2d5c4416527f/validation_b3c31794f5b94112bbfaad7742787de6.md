The code is confirmed. All cited lines match exactly. The bug is real:

- `add_entry` lines 210–211: local variables computed from pre-eviction `self.total_tx_size`
- `check_and_record_ancestors` line 618: calls `remove_entry_and_descendants` → `remove_entry` line 247 → `update_stat_for_remove_tx` which correctly decrements `self.total_tx_size`
- `add_entry` lines 218–219: stale local variables unconditionally overwrite `self.total_tx_size`, erasing the decrements
- `recompute_total_stat` is only triggered on underflow (checked_sub failure), never on inflation
- `limit_size` (pool.rs line 298) uses the inflated value to evict legitimate transactions, and is called after every `_submit_entry` (process.rs line 151)

---

Audit Report

## Title
Stale Local Variable Overwrites `total_tx_size` After Cell-Ref-Parent Eviction, Causing Permanent Inflation — (`tx-pool/src/component/pool_map.rs`)

## Summary
In `add_entry`, `updated_stat_for_add_tx` computes new pool totals into local variables before `check_and_record_ancestors` runs. When that function evicts `cell_ref_parents`, each eviction correctly decrements `self.total_tx_size` via `update_stat_for_remove_tx`. However, the stale local variable — computed before any eviction — then unconditionally overwrites `self.total_tx_size` at line 218, erasing all decrements. The result is a permanently inflated `total_tx_size` that causes `limit_size` to prematurely evict legitimate transactions from the pool.

## Finding Description
The exact sequence in `add_entry` (lines 210–219 of `tx-pool/src/component/pool_map.rs`):

```
Line 210-211: local_total = self.total_tx_size + entry.size
              (computed via updated_stat_for_add_tx, which only reads self.total_tx_size)

Line 213:     check_and_record_ancestors(&mut entry)
                → eviction branch (line 603): triggered when
                  ancestors_count > max_ancestors_count
                  AND ancestors_count - cell_ref_parents.len() <= max_ancestors_count
                → remove_entry_and_descendants (line 618)
                → remove_entry (line 263)
                → update_stat_for_remove_tx (line 247)
                → self.total_tx_size -= evicted_tx.size   ← correct decrement

Line 218:     self.total_tx_size = local_total            ← OVERWRITES the decrement
```

`updated_stat_for_add_tx` (lines 711–729) is a pure read: it computes `self.total_tx_size + tx_size` and returns it without writing to `self`. `update_stat_for_remove_tx` (lines 733–758) does write to `self.total_tx_size` via `checked_sub`. After the eviction loop in `check_and_record_ancestors`, `self.total_tx_size` correctly equals `old_total − Σ(evicted_sizes)`. Line 218 then overwrites it with `old_total + new_tx.size`, inflating it by exactly `Σ(evicted_sizes)`.

The only recovery path, `recompute_total_stat` (lines 698–708), is only invoked when `checked_sub` underflows (lines 742–756), which never happens in the inflation scenario. The inflation is therefore permanent and accumulates across submissions.

## Impact Explanation
`limit_size` (pool.rs lines 298–328) loops while `self.pool_map.total_tx_size > self.config.max_tx_pool_size`, evicting the lowest-fee-rate pending transactions. It is called immediately after every successful `_submit_entry` (process.rs line 151). With an inflated `total_tx_size`, `limit_size` evicts legitimate transactions that would not have been evicted under the true pool size. Because the inflation accumulates with each trigger, an attacker can drive `total_tx_size` arbitrarily above the real value, causing continuous premature eviction of honest transactions and degrading pool throughput across the network.

This matches the allowed impact: **High — Vulnerabilities or bad designs which could cause CKB network congestion with few costs.**

## Likelihood Explanation
The trigger requires a new transaction whose ancestor count exceeds `max_ancestors_count` (default 25) but where the excess ancestors are all `cell_ref_parents`. An unprivileged attacker can construct this deliberately by submitting a chain of 25+ transactions where some intermediate transactions are also used as cell deps, then submitting a final transaction that references those cell-dep ancestors. This requires only valid transaction fees and no privileged access. The path is reachable via standard P2P transaction relay (`submit_remote_tx`) or RPC (`send_transaction`). The condition is repeatable, so the attacker can accumulate inflation across multiple submissions.

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

Alternatively, call `recompute_total_stat` after any eviction occurs, or subtract evicted sizes from the pre-computed local variable before assigning it.

## Proof of Concept
```
Setup:
  max_ancestors_count = 25
  max_tx_pool_size    = 1_000_000 bytes
  Pool contains tx0..tx23 (chain of 24 txs, each 1000 bytes)
  total_tx_size = 24_000

Submit tx_extra:
  - inputs: tx23's output
  - cell_dep: tx0's output
  - ancestors = {tx0..tx23} → ancestors_count = 25 (no eviction yet)

Submit tx_trigger:
  - ancestors_count = 26 > 25
  - cell_ref_parents = {tx0}, so 26 - 1 = 25 ≤ 25 → eviction branch taken

  updated_stat_for_add_tx: local_total = 24_000 + 1000 = 25_000
  check_and_record_ancestors evicts tx0:
    update_stat_for_remove_tx: self.total_tx_size = 24_000 - 1000 = 23_000
  Line 218: self.total_tx_size = 25_000  ← BUG (should be 24_000)

  Inflation = 1000 bytes (= evicted tx0 size)

Repeat N times → total_tx_size inflated by N × evicted_size
→ limit_size fires and evicts honest transactions even though real pool size is within bounds
```
The code confirms the claim exactly. The bug is real and the exploit path is valid.

Audit Report

## Title
Stale Pre-Eviction Totals Overwrite Eviction-Adjusted `total_tx_size`/`total_tx_cycles` in `PoolMap::add_entry` — (`tx-pool/src/component/pool_map.rs`)

## Summary
In `PoolMap::add_entry`, `updated_stat_for_add_tx` computes new totals into local variables before `check_and_record_ancestors` runs. When `check_and_record_ancestors` evicts conflicting transactions, it correctly decrements `self.total_tx_size` and `self.total_tx_cycles` in-place via `update_stat_for_remove_tx`. However, lines 218–219 then unconditionally overwrite those fields with the stale pre-eviction locals, permanently overstating both counters by the aggregate size and cycles of all evicted transactions. This causes the pool to believe it is fuller than it actually is, triggering spurious evictions of legitimate transactions and premature `Reject::Full` rejections of incoming transactions.

## Finding Description
The exact sequence in `add_entry` (lines 200–221):

1. **Lines 210–211**: `updated_stat_for_add_tx(entry.size, entry.cycles)` computes `total_tx_size = self.total_tx_size + entry.size` and `total_tx_cycles = self.total_tx_cycles + entry.cycles` into **local variables**. `self.total_tx_size` and `self.total_tx_cycles` are not yet modified.

2. **Line 213**: `check_and_record_ancestors(&mut entry)` is called. When `ancestors_count > max_ancestors_count` but `ancestors_count - cell_ref_parents.len() <= max_ancestors_count` (lines 603–625), it calls `remove_entry_and_descendants` in a loop. Each call chains to `remove_entry` (lines 235–250), which calls `update_stat_for_remove_tx` (lines 733–757), **directly mutating `self.total_tx_size` and `self.total_tx_cycles` in-place**.

3. **Lines 218–219**: The stale locals (computed before any eviction) are unconditionally written back to `self.total_tx_size` and `self.total_tx_cycles`, discarding all eviction-driven decrements.

`update_stat_for_remove_tx` itself acknowledges accounting inaccuracy in its doc comment (line 731–732). The overwrite at lines 218–219 is the concrete mechanism that makes the inaccuracy permanent and exploitable.

Existing guards do not prevent this: `updated_stat_for_add_tx` only checks for integer overflow/fullness at the moment of the pre-computation call; it has no awareness of subsequent evictions. `limit_size` and `updated_stat_for_add_tx` both consume `self.pool_map.total_tx_size` directly and trust it to be accurate.

## Impact Explanation
`total_tx_size` is the authoritative counter for two critical admission paths:

- **`limit_size`** (pool.rs lines 298–307): loops `while self.pool_map.total_tx_size > self.config.max_tx_pool_size`, evicting legitimate pending/proposed transactions. With an overstated counter, this loop evicts transactions that would otherwise fit, degrading pool throughput.
- **`updated_stat_for_add_tx`** (pool_map.rs lines 716–721): rejects incoming transactions with `Reject::Full` when the counter overflows. With an overstated counter, valid transactions are rejected prematurely.

The combined effect is a sustained, externally-triggerable DoS against the transaction pool: legitimate transactions are evicted and new submissions are rejected even when the pool has real capacity. This matches the **High** impact class: *"Vulnerabilities or bad designs which could cause CKB network congestion with few costs."*

## Likelihood Explanation
The eviction path in `check_and_record_ancestors` is reachable by any unprivileged peer via `send_transaction` RPC or P2P relay. The attacker must:

1. Submit a set of transactions that share a cell dep, populating `cell_ref_parents` in the pool.
2. Submit a new transaction whose ancestor count exceeds `max_ancestors_count` (default 25) but whose `cell_ref_parents` count is large enough to satisfy the eviction condition at line 603.

No privileged access, majority hashpower, or social engineering is required. The condition is deterministic and repeatable: each successful trigger permanently inflates `total_tx_size` by the aggregate size of evicted transactions, and the attack can be repeated to compound the overstatement.

## Recommendation
Move the accounting update to **after** `check_and_record_ancestors` completes, so eviction-driven decrements are already reflected before the new entry's contribution is added. Replace lines 210–211 and 218–219 with a post-eviction addition:

```rust
// Remove pre-computation of total_tx_size/total_tx_cycles before check_and_record_ancestors.
// After check_and_record_ancestors returns (evictions already applied to self.*):
evicts = self.check_and_record_ancestors(&mut entry)?;
// Now add the new entry's contribution on top of the already-adjusted self fields:
self.total_tx_size = self.total_tx_size.checked_add(entry.size).ok_or_else(|| {
    Reject::Full(format!("tx-pool total_tx_size {} overflows by add {}", self.total_tx_size, entry.size))
})?;
self.total_tx_cycles = self.total_tx_cycles.checked_add(entry.cycles).ok_or_else(|| {
    Reject::Full(format!("tx-pool total_tx_cycles {} overflows by add {}", self.total_tx_cycles, entry.cycles))
})?;
```

Alternatively, move the `updated_stat_for_add_tx` call (and its overflow check) to after line 213, operating on the post-eviction `self` state.

## Proof of Concept
Concrete accounting trace (pool initially holds 3 txs, total size 300):

| Step | Event | `self.total_tx_size` |
|---|---|---|
| Initial | Pool has 3 txs, total size 300 | 300 |
| Line 210–211 | `updated_stat_for_add_tx(50, …)` → local `total_tx_size = 350` | 300 (unchanged) |
| Line 213 | `check_and_record_ancestors` evicts 2 txs (size 80 each) via `update_stat_for_remove_tx` | 300 − 80 − 80 = **140** |
| Lines 218–219 | Stale local written back | **350** (correct value: 190) |

To reproduce as a unit test: construct a `PoolMap` with `max_ancestors_count = 2`, insert transactions sharing a cell dep to populate `cell_ref_parents`, then insert a new transaction that triggers the eviction branch. Assert `pool_map.total_tx_size == pool_map.recompute_total_stat().0` after `add_entry` returns — this assertion will fail, confirming the overstatement.
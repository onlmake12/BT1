Looking at the actual code to verify the claim before rendering a verdict.

The key functions confirmed from the code:

- `updated_stat_for_add_tx` at line 711 takes `&self` (immutable reference) and returns a new computed value — it does **not** modify `self.total_tx_size`.
- `remove_entry` at line 247 calls `self.update_stat_for_remove_tx(entry.inner.size, entry.inner.cycles)` with no return value captured — it modifies `self.total_tx_size` in place.
- `add_entry` lines 210–219 confirm the ordering: local capture → eviction (mutates self) → overwrite with stale local.
- `limit_size()` at line 298 confirms `total_tx_size` is the sole guard.

The execution trace is exactly as claimed. The bug is real and confirmed.

---

Audit Report

## Title
`total_tx_size`/`total_tx_cycles` Permanently Inflated After Ancestor-Eviction in `add_entry` — (File: tx-pool/src/component/pool_map.rs)

## Summary
In `PoolMap::add_entry`, `updated_stat_for_add_tx` captures the pre-eviction sum into a local variable, then `check_and_record_ancestors` evicts transactions and correctly subtracts their sizes from `self.total_tx_size` via `update_stat_for_remove_tx`. However, the final assignment `self.total_tx_size = total_tx_size` overwrites those subtractions with the stale pre-eviction value, permanently inflating `total_tx_size` by the size of every evicted transaction. Because `limit_size()` uses `total_tx_size` as its sole eviction guard, the pool will evict valid transactions to compensate for phantom capacity that does not exist.

## Finding Description
In `add_entry` (`tx-pool/src/component/pool_map.rs`, lines 200–221):

```
Step 1 (line 210-211): updated_stat_for_add_tx(&self, ...) → takes &self (immutable),
        returns local (total_tx_size = X + new_size). self.total_tx_size unchanged at X.

Step 2 (line 213): check_and_record_ancestors → remove_entry_and_descendants →
        remove_entry (line 247) → update_stat_for_remove_tx(&mut self, S, ...) →
        self.total_tx_size = X − S.  (correct eviction accounting)

Step 3 (lines 218-219): self.total_tx_size = total_tx_size  →  X + new_size
        (stale local, eviction never reflected)
```

Correct final value: `X − S + new_size`. Actual committed value: `X + new_size`. Inflation per trigger: `S` (size of all evicted transactions). The eviction path in `check_and_record_ancestors` (lines 603–625) is reached when `ancestors_count > max_ancestors_count` but `ancestors_count − cell_ref_parents.len() ≤ max_ancestors_count`, causing `remove_entry_and_descendants` to be called for each cell-ref parent. Each such call reaches `remove_entry` (line 247) which calls `update_stat_for_remove_tx` — but those subtractions are clobbered at lines 218–219.

Existing guards do not prevent this: `updated_stat_for_add_tx` only checks for overflow of the pre-eviction total, not the post-eviction total, so it cannot detect or prevent the stale overwrite.

## Impact Explanation
`total_tx_size` is the sole guard used by `limit_size()` (pool.rs, line 298) to enforce the pool's memory cap. With `total_tx_size` inflated by `S` per trigger, `limit_size()` sees the pool as over-capacity and evicts additional valid pending transactions. Each subsequent submission that triggers the ancestor-eviction path compounds the inflation additively. A sustained series of such submissions can drain the mempool of legitimate transactions, preventing them from being proposed or committed to blocks. The `tx_pool_info` RPC (service.rs, lines 1089–1090) also exposes the corrupted counters, misleading operators and monitoring systems. This matches: **High — Vulnerabilities or bad designs which could cause CKB network congestion with few costs.**

## Likelihood Explanation
The trigger requires `ancestors_count > max_ancestors_count` while `ancestors_count − cell_ref_parents.len() ≤ max_ancestors_count`. An unprivileged submitter can construct this by seeding the pool with a chain of transactions sharing a cell dep (making them `cell_ref_parents` of a future transaction), then submitting a transaction that pushes the ancestor count one above `max_ancestors_count`. The default `max_ancestors_count` is 25, reachable with 25 chained transactions. No privileged access, key material, or majority hashpower is required. The attack is repeatable: after each trigger the attacker can re-seed and trigger again, compounding the inflation. Transaction fees are required but are modest relative to the disruption caused.

## Recommendation
Move `updated_stat_for_add_tx` to **after** `check_and_record_ancestors` completes, so the addition is applied to the already-corrected `self.total_tx_size`:

```rust
evicts = self.check_and_record_ancestors(&mut entry)?;
// self.total_tx_size now reflects evictions; add the new tx on top
let (total_tx_size, total_tx_cycles) =
    self.updated_stat_for_add_tx(entry.size, entry.cycles)?;
self.record_entry_edges(&entry)?;
self.insert_entry(&entry, status);
self.record_entry_descendants(&entry);
self.track_entry_statics(None, Some(status));
self.total_tx_size = total_tx_size;
self.total_tx_cycles = total_tx_cycles;
```

Alternatively, assert consistency post-insertion using `recompute_total_stat()` in debug/test builds to catch future regressions.

## Proof of Concept
1. Submit transactions `T1 → T2 → … → T24` where each `Ti` uses the output of `T(i−1)` as a cell dep, making them `cell_ref_parents` of any future transaction referencing `T24`'s output.
2. Submit `T25` referencing `T24`'s output as a cell dep. `ancestors_count = 25 = max_ancestors_count`; no eviction.
3. Submit `T26` also referencing `T24`'s output as a cell dep. `ancestors_count = 26 > 25`, but `26 − 1 = 25 ≤ 25`, so `check_and_record_ancestors` evicts `T25` (size `S`):
   - `update_stat_for_remove_tx(S, cycles_S)` → `self.total_tx_size -= S` ✓
   - `self.total_tx_size = total_tx_size` (pre-eviction value) → `self.total_tx_size += S` ✗
4. Query `tx_pool_info` RPC: observe `total_tx_size` exceeds the sum of actual entry sizes by `S`.
5. Observe `limit_size()` evicting valid transactions to compensate for the phantom inflation.
6. Repeat steps 1–3 to compound the inflation. A unit test can assert `pool_map.total_tx_size == pool_map.recompute_total_stat().unwrap().0` after each `add_entry` call to reproduce the invariant violation deterministically.
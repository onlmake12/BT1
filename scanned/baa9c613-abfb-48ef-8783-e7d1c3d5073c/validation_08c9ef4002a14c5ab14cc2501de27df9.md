The code confirms the claim exactly. Let me verify the `limit_size` usage and pool.rs for completeness.

Audit Report

## Title
`total_tx_size`/`total_tx_cycles` Permanently Inflated by Stale Snapshot Overwrite After Eviction in `add_entry` - (File: tx-pool/src/component/pool_map.rs)

## Summary

In `PoolMap::add_entry`, a pre-eviction snapshot of `total_tx_size` and `total_tx_cycles` is captured at lines 210–211, then unconditionally written back at lines 218–219 after `check_and_record_ancestors` (line 213) may have already decremented those counters via `update_stat_for_remove_tx`. The decrements are silently discarded, permanently inflating the pool's reported resource usage. An unprivileged caller can exploit this repeatedly to make the pool appear full, causing all subsequent `send_transaction` calls to be rejected with `PoolIsFull`.

## Finding Description

The exact sequence in `add_entry` (lines 200–221):

1. **Lines 210–211**: `updated_stat_for_add_tx` computes `total_tx_size = self.total_tx_size + entry.size` — a snapshot taken *before* any evictions.
2. **Line 213**: `check_and_record_ancestors` is called. When the incoming transaction has more than `max_ancestors_count` ancestors but the count can be reduced by evicting `cell_ref_parents`, the eviction loop at lines 616–625 calls `remove_entry_and_descendants` → `remove_entry` (line 263) → `update_stat_for_remove_tx` (line 247), which correctly decrements `self.total_tx_size` and `self.total_tx_cycles` in-place.
3. **Lines 218–219**: `self.total_tx_size = total_tx_size` and `self.total_tx_cycles = total_tx_cycles` unconditionally overwrite the live counters with the pre-eviction snapshot, erasing every decrement applied in step 2.

The eviction path in `check_and_record_ancestors` is triggered when `ancestors_count > max_ancestors_count` but `ancestors_count - cell_ref_parents.len() <= max_ancestors_count` (line 603). This is a fully reachable code path, not a theoretical one.

`updated_stat_for_add_tx` (lines 711–729) also enforces the pool-full check against the pre-eviction `self.total_tx_size`, meaning the overflow guard fires on the wrong baseline — but this is secondary to the overwrite bug.

## Impact Explanation

`total_tx_size` is the authoritative counter driving two enforcement points:

- **`limit_size`** (pool.rs line 298): evicts entries while `total_tx_size > max_tx_pool_size`. An inflated counter triggers unnecessary eviction of already-accepted legitimate transactions.
- **`updated_stat_for_add_tx`** (pool_map.rs line 716): rejects new submissions with `Reject::Full` when the counter exceeds the pool limit. An inflated counter causes valid transactions to be rejected even when actual pool space is available.

Each successful exploit inflates `total_tx_size` by the serialized size of the evicted transaction(s). The inflation is permanent until the pool is restarted. Repeated exploitation accumulates inflation, eventually making the pool appear full when it is nearly empty. This constitutes a sustained, low-cost denial-of-service against transaction submission to the targeted node, matching the allowed impact: **"Vulnerabilities or bad designs which could cause CKB network congestion with few costs" — High (10001–15000 points)**.

## Likelihood Explanation

The trigger condition is fully constructible by any unprivileged `send_transaction` caller with no special privileges, no majority hashpower, and no victim mistakes. The attacker only needs to craft a valid transaction chain that exceeds `max_ancestors_count` (default 25) while having at least one `cell_ref_parent` that can be evicted to bring the count within the limit. This is a standard transaction graph construction. The attack is repeatable with fresh transaction chains, allowing cumulative inflation.

## Recommendation

Move the size/cycle accounting to *after* `check_and_record_ancestors` returns, so the addition is applied against the already-decremented (post-eviction) counters:

```rust
pub(crate) fn add_entry(&mut self, mut entry: TxEntry, status: Status) -> Result<(bool, HashSet<TxEntry>), Reject> {
    let tx_short_id = entry.proposal_short_id();
    let mut evicts = Default::default();
    if self.entries.get_by_id(&tx_short_id).is_some() {
        return Ok((false, evicts));
    }
    // Evictions first — they decrement self.total_tx_size in place
    evicts = self.check_and_record_ancestors(&mut entry)?;
    self.record_entry_edges(&entry)?;
    self.insert_entry(&entry, status);
    self.record_entry_descendants(&entry);
    self.track_entry_statics(None, Some(status));
    // Now add the new entry's contribution against the post-eviction counters
    let (total_tx_size, total_tx_cycles) =
        self.updated_stat_for_add_tx(entry.size, entry.cycles)?;
    self.total_tx_size = total_tx_size;
    self.total_tx_cycles = total_tx_cycles;
    Ok((true, evicts))
}
```

This ensures the overflow/full check in `updated_stat_for_add_tx` fires against the correct post-eviction baseline and no decrements are overwritten.

## Proof of Concept

```
Setup: max_ancestors_count = 25, pool empty, total_tx_size = 0

1. Submit tx_base (2 outputs: out_Y, out_X, size=200) → total_tx_size = 200
2. Submit tx1→tx2→…→tx25 chained from out_Y (each size=200) → total_tx_size = 5200
3. Submit txA using out_X as cell_dep (size=500) → total_tx_size = 5700
4. Submit txNew spending tx25.output AND out_X:
   - ancestors = {tx_base, tx1..tx25} → ancestors_count = 26 > 25
   - cell_ref_parents = {txA}; 26 - 1 = 25 ≤ 25 → eviction path fires
   - snapshot (line 210): total_tx_size_snap = 5700 + 200 = 5900
   - remove_entry_and_descendants(txA): self.total_tx_size = 5700 - 500 = 5200
   - txNew inserted
   - line 218: self.total_tx_size = 5900  ← stale snapshot overwrites, losing -500

Expected total_tx_size: 5200 (tx_base + tx1..tx25) + 200 (txNew) = 5400
Actual total_tx_size:   5900  ← inflated by 500 (txA's size)

Repeating N times inflates total_tx_size by N×500.
After enough repetitions, updated_stat_for_add_tx rejects all new submissions
with Reject::Full even though the pool has ample real space.
```

A unit test can be written directly against `PoolMap::add_entry` by constructing the described transaction graph, calling `add_entry` for `txNew`, and asserting `pool_map.total_tx_size` equals the sum of sizes of entries actually present in `pool_map.entries`.
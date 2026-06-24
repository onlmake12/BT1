Audit Report

## Title
`total_tx_size` Overwritten with Pre-Eviction Snapshot in `PoolMap::add_entry`, Causing Permanent Inflation and Tx-Pool DoS — (File: `tx-pool/src/component/pool_map.rs`)

## Summary

In `PoolMap::add_entry`, `updated_stat_for_add_tx` captures a snapshot of `total_tx_size + new_size` before `check_and_record_ancestors` runs. When that function evicts `cell_ref_parents` via `remove_entry_and_descendants`, `update_stat_for_remove_tx` correctly decrements `self.total_tx_size`. However, lines 218–219 then unconditionally overwrite `self.total_tx_size` with the stale pre-eviction snapshot, permanently discarding the decrement. Repeated triggering inflates `total_tx_size` until it permanently exceeds `max_tx_pool_size`, causing `limit_size` to evict every newly submitted transaction and rendering the tx-pool non-functional.

## Finding Description

**Root cause — write-ordering bug in `add_entry` (lines 200–221):**

```
// Line 210-211: snapshot taken BEFORE evictions
let (total_tx_size, total_tx_cycles) =
    self.updated_stat_for_add_tx(entry.size, entry.cycles)?;
// total_tx_size == self.total_tx_size + entry.size  (call this X + S)

// Line 213: may call remove_entry_and_descendants -> update_stat_for_remove_tx
// which DECREMENTS self.total_tx_size by evicted_size
evicts = self.check_and_record_ancestors(&mut entry)?;
// self.total_tx_size is now X - evicted_size

// Lines 218-219: OVERWRITES with stale snapshot
self.total_tx_size = total_tx_size;   // sets to X + S, not X - evicted_size + S
self.total_tx_cycles = total_tx_cycles;
```

`updated_stat_for_add_tx` (lines 711–728) is a pure read that returns `(self.total_tx_size + tx_size, self.total_tx_cycles + cycles)` — it does not mutate state. `update_stat_for_remove_tx` (lines 733–758) directly writes `self.total_tx_size -= tx_size`. Because the snapshot is taken first and written back last, every eviction decrement is silently discarded.

**Eviction path reachability (lines 603–625):**

The eviction branch inside `check_and_record_ancestors` fires when:
- `ancestors_count > max_ancestors_count` (default 25), AND
- `ancestors_count - cell_ref_parents.len() <= max_ancestors_count`

Under these conditions, `remove_entry_and_descendants` is called for each `cell_ref_parent` until `ancestors_count` drops to the limit. Each removed entry triggers `update_stat_for_remove_tx`, which decrements `self.total_tx_size` — a decrement that is then erased by the overwrite at line 218.

**Inflation per trigger:** `sum(size of evicted cell_ref_parents)`.

**Existing guards are insufficient:** `updated_stat_for_add_tx` does perform an overflow check (returning `Reject::Full` on overflow), but this only guards against arithmetic overflow, not against the logical overwrite of post-eviction state. There is no guard that re-reads `self.total_tx_size` after eviction before writing back.

## Impact Explanation

`limit_size` (pool.rs line 298) loops `while self.pool_map.total_tx_size > self.config.max_tx_pool_size`, evicting the lowest-fee-rate entries. Once `total_tx_size` is inflated past `max_tx_pool_size` (default 180 MB), every call to `submit_entry` triggers `limit_size`, which evicts the just-inserted transaction or other legitimate transactions. The tx-pool becomes permanently unable to accept new submissions, and miner block assembly is blocked.

This matches **High (10001–15000 points): Vulnerabilities which could easily crash a CKB node** — the tx-pool subsystem of the targeted node is rendered non-functional. If the attack is applied to a significant fraction of nodes (which requires only repeated cheap RPC calls), it also matches **High: Vulnerabilities or bad designs which could cause CKB network congestion with few costs**.

## Likelihood Explanation

The trigger requires no privileged access, no majority hashpower, and no Sybil attack. Any unprivileged user with access to `send_raw_transaction` (RPC or P2P relay) can:

1. Build a chain of 25 input-chain ancestors in the pool.
2. Submit a set of `cell_ref_parent` transactions (using a shared cell-dep output).
3. Submit a transaction that has both the 25-deep input chain and the cell-dep reference, pushing `ancestors_count` just above 25 while `ancestors_count - k ≤ 25`.
4. The eviction fires, inflating `total_tx_size` by the sum of evicted sizes.
5. Repeat until `total_tx_size > max_tx_pool_size`.

The setup is deterministic and repeatable. The cost is proportional to the number of transactions submitted, which can be minimized by using large transactions to maximize inflation per iteration.

## Recommendation

Remove the pre-computed snapshot pattern entirely. Instead, increment `self.total_tx_size` and `self.total_tx_cycles` **after** `check_and_record_ancestors` completes, using the current (post-eviction) field values:

```rust
// Replace lines 210-211 and 218-219 with:

// Before check_and_record_ancestors: only do overflow check, don't store snapshot
self.total_tx_size.checked_add(entry.size).ok_or_else(|| {
    Reject::Full(format!("tx-pool total_tx_size {} overflows by add {}", self.total_tx_size, entry.size))
})?;
self.total_tx_cycles.checked_add(entry.cycles).ok_or_else(|| {
    Reject::Full(format!("tx-pool total_tx_cycles {} overflows by add {}", self.total_tx_cycles, entry.cycles))
})?;

evicts = self.check_and_record_ancestors(&mut entry)?;
self.record_entry_edges(&entry)?;
self.insert_entry(&entry, status);
self.record_entry_descendants(&entry);
self.track_entry_statics(None, Some(status));

// Increment AFTER evictions, based on current (post-eviction) self.total_tx_size
self.total_tx_size = self.total_tx_size
    .checked_add(entry.size)
    .expect("overflow checked above");
self.total_tx_cycles = self.total_tx_cycles
    .checked_add(entry.cycles)
    .expect("overflow checked above");
```

This ensures the post-eviction decrements are preserved before the new entry's size is added.

## Proof of Concept

**Minimal manual steps (default config: `max_ancestors_count=25`, `max_tx_pool_size=180_000_000`):**

1. Submit `T0` (output used as cell-dep by subsequent txs).
2. Submit `T1…T5`, each referencing `T0`'s output as a cell-dep (~1 MB each). These become `cell_ref_parents` of any tx that also uses `T0` as cell-dep.
3. Build a 25-deep input chain `C1→…→C25` in the pool.
4. Submit `Tnew` spending `C25`'s output (25 input-chain ancestors) and using `T0` as cell-dep. `ancestors_count = 26 > 25`, `26 - 5 = 21 ≤ 25` → eviction branch fires.
5. `check_and_record_ancestors` evicts some of `T1…T5` via `remove_entry_and_descendants` → `update_stat_for_remove_tx` decrements `self.total_tx_size` by ~N MB.
6. Lines 218–219 overwrite `self.total_tx_size` with the pre-eviction snapshot, losing the ~N MB decrement.
7. Repeat steps 3–6 ~90 times → `total_tx_size` exceeds 180 MB while actual pool occupancy is far lower.
8. Every subsequent `send_raw_transaction` triggers `limit_size`, which immediately evicts the submitted tx → **tx-pool DoS**.

**Unit test invariant to add:** After any `add_entry` call, assert `pool_map.total_tx_size == pool_map.recompute_total_stat().0`. This invariant will fail on the first eviction-triggering `add_entry` with the current code.
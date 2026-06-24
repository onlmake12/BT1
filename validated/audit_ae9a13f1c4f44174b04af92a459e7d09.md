Audit Report

## Title
`add_entry()` Overwrites Post-Eviction Pool Size/Cycle Totals with Stale Pre-Eviction Snapshot — (File: `tx-pool/src/component/pool_map.rs`)

## Summary
In `PoolMap::add_entry()`, local variables capturing the pre-eviction totals (`total_tx_size`, `total_tx_cycles`) are computed before `check_and_record_ancestors` runs, which may internally call `remove_entry_and_descendants` → `remove_entry` → `update_stat_for_remove_tx`, correctly modifying `self.total_tx_size` and `self.total_tx_cycles` in place. The final write-back on lines 218–219 then unconditionally overwrites those correct post-eviction values with the stale pre-eviction snapshot, permanently inflating the pool's accounting totals by the sizes and cycles of every evicted transaction. These inflated totals are directly exposed via RPC and used for pool admission gating.

## Finding Description
The exact code sequence in `add_entry()` (lines 200–221):

```
Line 210-211: let (total_tx_size, total_tx_cycles) =
                  self.updated_stat_for_add_tx(entry.size, entry.cycles)?;
              // LOCAL snapshot: old_total + entry.size (pre-eviction)

Line 213:     evicts = self.check_and_record_ancestors(&mut entry)?;
              // May call remove_entry_and_descendants → remove_entry →
              // update_stat_for_remove_tx, which writes:
              //   self.total_tx_size  -= evicted_size   (correct)
              //   self.total_tx_cycles -= evicted_cycles (correct)

Line 218-219: self.total_tx_size  = total_tx_size;   // OVERWRITES correct value
              self.total_tx_cycles = total_tx_cycles; // OVERWRITES correct value
```

`updated_stat_for_add_tx` (lines 711–729) is a `&self` method that returns new values as local variables — it does not mutate `self`. It only checks for integer overflow via `checked_add`, not against `max_tx_pool_size`. The eviction path in `check_and_record_ancestors` (lines 603–625) calls `remove_entry_and_descendants` when `ancestors_count.saturating_sub(cell_ref_parents.len()) <= self.max_ancestors_count`, which chains to `remove_entry` (lines 235–249), which calls `update_stat_for_remove_tx` (lines 733–758) that directly mutates `self.total_tx_size` and `self.total_tx_cycles`. The subsequent unconditional write-back on lines 218–219 discards those correct mutations.

The `recompute_total_stat` fallback (lines 698–708) is only triggered inside `update_stat_for_remove_tx` on underflow — it is never triggered by inflation, so the ghost inflation persists indefinitely. The inflated totals are directly read for RPC reporting at `service.rs` lines 1089–1090 (`total_tx_size: tx_pool.pool_map.total_tx_size`, `total_tx_cycles: tx_pool.pool_map.total_tx_cycles`).

## Impact Explanation
The inflated `total_tx_size` and `total_tx_cycles` are permanently incorrect after each eviction-triggering `add_entry` call. Each such event adds the evicted transactions' sizes to the ghost inflation. The pool's RPC-reported statistics become incorrect immediately. More critically, pool admission checks that gate on `total_tx_size` against `max_tx_pool_size` will prematurely reject legitimate transactions once the cumulative inflation exceeds the configured limit — effectively a local mempool denial-of-service. An attacker who can repeatedly trigger the eviction path can drive `total_tx_size` above `max_tx_pool_size` while the actual pool occupancy remains well below the limit, causing all subsequent `send_transaction` RPC calls to be rejected. This matches **High — Vulnerabilities or bad designs which could cause CKB network congestion with few costs**, as a node whose mempool permanently rejects all incoming transactions cannot participate in transaction propagation.

## Likelihood Explanation
The eviction branch requires: (1) a new transaction whose ancestor count exceeds `max_ancestors_count`, and (2) some of those ancestors are `cell_ref_parents` (transactions using a cell as `cell_dep` that the new transaction consumes as an input). An unprivileged external caller via `send_transaction` RPC can deliberately construct this: first submit transactions referencing a specific live cell as `cell_dep`, then submit a long-chain transaction spending that cell as an input. This is repeatable, requires no special privileges, and can be scripted to trigger the inflation multiple times until the pool is permanently wedged.

## Recommendation
Remove the pre-eviction snapshot pattern. Instead, apply the new entry's contribution to `self.total_tx_size` and `self.total_tx_cycles` directly after evictions have already updated them via `update_stat_for_remove_tx`:

```rust
pub(crate) fn add_entry(
    &mut self,
    mut entry: TxEntry,
    status: Status,
) -> Result<(bool, HashSet<TxEntry>), Reject> {
    let tx_short_id = entry.proposal_short_id();
    let mut evicts = Default::default();
    if self.entries.get_by_id(&tx_short_id).is_some() {
        return Ok((false, evicts));
    }
    // Validate limits before any mutation (no state change yet)
    self.updated_stat_for_add_tx(entry.size, entry.cycles)?;
    trace!("pool_map.add_{:?} {}", status, entry.transaction().hash());
    evicts = self.check_and_record_ancestors(&mut entry)?;
    self.record_entry_edges(&entry)?;
    self.insert_entry(&entry, status);
    self.record_entry_descendants(&entry);
    self.track_entry_statics(None, Some(status));
    // Apply new entry AFTER evictions have already updated totals
    self.total_tx_size = self.total_tx_size.saturating_add(entry.size);
    self.total_tx_cycles = self.total_tx_cycles.saturating_add(entry.cycles);
    Ok((true, evicts))
}
```

## Proof of Concept
**Setup:** `max_ancestors_count = 25`, pool holds 20 transactions (total size = 3,000 bytes), some using `cell_X` as `cell_dep`.

1. Submit `tx_A` (size = 500 bytes) spending `cell_X` as input with 24 in-pool ancestors → `ancestors_count = 25 > max_ancestors_count`, `cell_ref_parents` non-empty, eviction branch at line 603 is entered.
2. Three `cell_ref_parent` transactions (total size = 1,500 bytes) are evicted via `remove_entry_and_descendants`. `update_stat_for_remove_tx` correctly sets `self.total_tx_size = 3,000 − 1,500 = 1,500`.
3. Lines 218–219 overwrite: `self.total_tx_size = 3,000 + 500 = 3,500`. Correct value should be `1,500 + 500 = 2,000`. Ghost inflation = 1,500 bytes.
4. Repeat step 1 with fresh `cell_ref_parent` setups. After ~5 iterations, `total_tx_size` exceeds `max_tx_pool_size` while actual pool occupancy remains low. All subsequent `send_transaction` calls are rejected.

A unit test can be written against `PoolMap` directly: construct the eviction scenario, call `add_entry`, then assert `pool_map.total_tx_size == actual_sum_of_entry_sizes` using `recompute_total_stat()` as the ground truth comparator.
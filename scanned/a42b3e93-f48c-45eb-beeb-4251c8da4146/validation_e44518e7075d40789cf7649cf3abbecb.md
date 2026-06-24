Audit Report

## Title
Stale Pre-Eviction Snapshot Overwrites Live `total_tx_size`/`total_tx_cycles` in `add_entry` — (File: tx-pool/src/component/pool_map.rs)

## Summary
In `PoolMap::add_entry`, `updated_stat_for_add_tx` snapshots the new pool totals before `check_and_record_ancestors` runs. That function can evict existing entries via `remove_entry_and_descendants`, each of which correctly decrements `self.total_tx_size`/`self.total_tx_cycles` through `update_stat_for_remove_tx`. Immediately after, `add_entry` blindly overwrites those live fields with the stale pre-eviction snapshot, re-inflating the totals by the size and cycles of every evicted transaction. The inflation is cumulative and unbounded, causing `limit_size` to evict legitimate pending transactions and reject new submissions with `Reject::Full` even when the pool has real capacity.

## Finding Description

The exact execution order in `add_entry` (lines 200–221):

```
L210-211: (total_tx_size, total_tx_cycles) = updated_stat_for_add_tx(entry.size, entry.cycles)
          // snapshot = self.total_tx_size + entry.size  (pre-eviction)

L213:     evicts = check_and_record_ancestors(&mut entry)
          // may call remove_entry_and_descendants → remove_entry → update_stat_for_remove_tx
          // which DECREMENTS self.total_tx_size / self.total_tx_cycles for each evicted tx

L218-219: self.total_tx_size  = total_tx_size   // OVERWRITES the correctly-decremented live value
          self.total_tx_cycles = total_tx_cycles // OVERWRITES the correctly-decremented live value
```

`updated_stat_for_add_tx` (lines 711–729) is a pure read — it takes `&self` and returns a computed pair without mutating anything. It only guards against arithmetic overflow, not pool-size limits.

`check_and_record_ancestors` (lines 603–625) triggers the eviction path when `ancestors_count > max_ancestors_count` but the excess is entirely attributable to `cell_ref_parents`. It calls `remove_entry_and_descendants` (lines 252–264), which calls `remove_entry` (lines 235–249) for each removed entry. `remove_entry` calls `update_stat_for_remove_tx` at line 247, correctly decrementing `self.total_tx_size` and `self.total_tx_cycles`.

Lines 218–219 then overwrite those correctly-decremented values with the stale snapshot, silently re-adding the bytes and cycles of every evicted transaction. The evicted entries are gone from the pool but their accounting contribution is permanently re-injected.

`limit_size` (line 298) uses `self.pool_map.total_tx_size > self.config.max_tx_pool_size` as its sole guard. An inflated `total_tx_size` causes it to keep evicting legitimate pending transactions even though the pool is actually below the configured limit.

## Impact Explanation

This matches **High: Vulnerabilities or bad designs which could cause CKB network congestion with few costs.**

Each successful ancestor-eviction cycle inflates `total_tx_size` by the size of the evicted entries. The inflation is permanent and cumulative — it is never corrected unless `recompute_total_stat` is called (which only happens on underflow, not on this overwrite path). An attacker who repeats the pattern N times inflates the counter by ~N KB, eventually causing `limit_size` to continuously evict honest pending transactions and reject all new submissions with `Reject::Full`, even when the pool is nearly empty. Applied across multiple nodes simultaneously (the attack uses only the standard `send_transaction` RPC, which is relayed peer-to-peer), this degrades the network's ability to propagate and confirm transactions at low cost to the attacker.

## Likelihood Explanation

The trigger requires no privilege, no key material, and no majority hash power. Any RPC caller or peer can:
1. Submit a chain of up to `max_ancestors_count − 1` (default 24) transactions.
2. Submit one more transaction whose cell-dep references one of those in-pool transactions, pushing the ancestor count over the limit with `cell_ref_parents` non-empty.

Both steps use only `send_transaction`. The default `max_ancestors_count` of 25 is easily reachable on mainnet. The attack is repeatable indefinitely and each iteration compounds the inflation.

## Recommendation

Move the computation of `total_tx_size`/`total_tx_cycles` to **after** `check_and_record_ancestors` completes, so any evictions are already reflected in `self.total_tx_size`/`self.total_tx_cycles` before the new entry's contribution is added:

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
    // REMOVED: early snapshot before evictions
    trace!("pool_map.add_{:?} {}", status, entry.transaction().hash());
    evicts = self.check_and_record_ancestors(&mut entry)?;
    self.record_entry_edges(&entry)?;
    self.insert_entry(&entry, status);
    self.record_entry_descendants(&entry);
    self.track_entry_statics(None, Some(status));
    // Compute AFTER evictions have already updated self.total_tx_*:
    let (total_tx_size, total_tx_cycles) =
        self.updated_stat_for_add_tx(entry.size, entry.cycles)?;
    self.total_tx_size = total_tx_size;
    self.total_tx_cycles = total_tx_cycles;
    Ok((true, evicts))
}
```

Alternatively, call `recompute_total_stat()` unconditionally at the end of `add_entry` whenever `evicts` is non-empty.

## Proof of Concept

Assume `max_tx_pool_size = 180 MB`, `max_ancestors_count = 25`, each transaction ~1 KB.

1. Submit 24 chained transactions `T1 → T2 → … → T24`. Pool `total_tx_size = 24 KB` (correct).
2. Submit `T25` whose cell-dep references `T1` (ancestor count = 26 > 25, `cell_ref_parents = {T1}`).
3. `check_and_record_ancestors` evicts `T1`: `update_stat_for_remove_tx` sets `self.total_tx_size = 23 KB`.
4. `add_entry` executes `self.total_tx_size = total_tx_size` where `total_tx_size` was snapshotted as `24 + 1 = 25 KB`.
5. Pool now reports `total_tx_size = 25 KB` but actually holds `T2…T24 + T25 = 24 KB`. Inflation = 1 KB.
6. Repeat steps 1–5 N times. After N iterations, reported size exceeds real size by ~N KB.
7. Once reported size exceeds `max_tx_pool_size`, `limit_size` begins evicting honest transactions and `updated_stat_for_add_tx` starts returning `Reject::Full` for all new submissions, even though the pool is nearly empty.

A unit test can verify this directly: after one eviction cycle, assert `pool_map.total_tx_size == pool_map.entries.iter().map(|e| e.size).sum()` — the assertion will fail on the current code.
Audit Report

## Title
`total_tx_size`/`total_tx_cycles` Permanently Inflated by Cell-Dep Eviction in `add_entry()` — (File: tx-pool/src/component/pool_map.rs)

## Summary
In `PoolMap::add_entry()`, `updated_stat_for_add_tx()` snapshots `self.total_tx_size + entry.size` into locals before `check_and_record_ancestors()` runs. When that function evicts cell-dep-referencing transactions, `update_stat_for_remove_tx()` correctly decrements `self.total_tx_size` in place, but lines 218–219 unconditionally overwrite those decremented values with the stale pre-eviction snapshot. The result is a permanent inflation of `total_tx_size` and `total_tx_cycles` by the aggregate size/cycles of every evicted transaction, which an unprivileged attacker can repeat to drive the pool into a sustained denial-of-service state.

## Finding Description
In `add_entry()` (lines 200–221 of `tx-pool/src/component/pool_map.rs`):

- **Lines 210–211**: `updated_stat_for_add_tx(entry.size, entry.cycles)` reads `self.total_tx_size` and returns `self.total_tx_size + entry.size` into the local `total_tx_size`. This is a pure read; it does not mutate `self`.
- **Line 213**: `check_and_record_ancestors(&mut entry)` is called. When `ancestors_count > max_ancestors_count` but `ancestors_count - cell_ref_parents.len() <= max_ancestors_count` (lines 603–625), it calls `remove_entry_and_descendants()` for each evict candidate. `remove_entry_and_descendants()` (lines 252–264) calls `remove_entry()` per entry, which calls `update_stat_for_remove_tx()` (lines 733–741), directly writing the decremented value back to `self.total_tx_size`.
- **Lines 218–219**: `self.total_tx_size = total_tx_size` and `self.total_tx_cycles = total_tx_cycles` unconditionally overwrite `self.total_tx_size` with the stale pre-eviction snapshot.

The net effect: if evictions removed aggregate size `S_evict`, `self.total_tx_size` ends up as `T + entry.size` instead of the correct `T - S_evict + entry.size`, inflated by exactly `S_evict`. No subsequent code path corrects this; the inflation is permanent for the lifetime of the pool instance.

The `recompute_total_stat()` fallback inside `update_stat_for_remove_tx()` (lines 743–749) only fires on underflow during removal and is immediately overwritten by lines 218–219 anyway, so it provides no protection here.

## Impact Explanation
The inflated counter has three concrete downstream effects:

1. **Unnecessary eviction of legitimate transactions**: `limit_size()` loops `while self.pool_map.total_tx_size > self.config.max_tx_pool_size` and evicts lowest-fee-rate entries. With an inflated counter, real transactions that should remain are evicted.
2. **False `Reject::Full` for incoming transactions**: `updated_stat_for_add_tx()` uses `self.total_tx_size` as its base; subsequent honest submissions are rejected even when actual occupancy is well below `max_tx_pool_size`.
3. **Misleading RPC state**: `get_pool_info` reads `total_tx_size` directly from `pool_map.total_tx_size`, returning incorrect values to all callers.

The attack is repeatable: each trigger inflates the counter by `S_evict`. After enough repetitions the pool permanently rejects all incoming transactions with `Reject::Full` and continuously evicts its own contents. This matches the allowed impact: **High — Vulnerabilities or bad designs which could cause CKB network congestion with few costs**.

## Likelihood Explanation
No privileged access, key material, or majority hash power is required. Any unprivileged peer can submit transactions. The trigger condition — many in-pool transactions sharing a cell dep, followed by a transaction that spends that cell dep's output — is demonstrated by the existing integration test `TxPoolLimitAncestorCount`. The attacker pays transaction fees for approximately 2001 transactions per trigger, but the inflation is permanent per trigger and the attack is repeatable, making the cost-to-impact ratio low.

## Recommendation
Move the `updated_stat_for_add_tx()` call to **after** `check_and_record_ancestors()` completes, so the snapshot is taken from the already-decremented `self.total_tx_size`:

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
    trace!("pool_map.add_{:?} {}", status, entry.transaction().hash());
    evicts = self.check_and_record_ancestors(&mut entry)?;
    // Compute totals AFTER evictions have decremented self.total_tx_size
    let (total_tx_size, total_tx_cycles) =
        self.updated_stat_for_add_tx(entry.size, entry.cycles)?;
    self.record_entry_edges(&entry)?;
    self.insert_entry(&entry, status);
    self.record_entry_descendants(&entry);
    self.track_entry_statics(None, Some(status));
    self.total_tx_size = total_tx_size;
    self.total_tx_cycles = total_tx_cycles;
    Ok((true, evicts))
}
```

This ensures the final written value is `(T - S_evict) + entry.size` as intended.

## Proof of Concept
1. Submit 2,000 transactions each referencing `tx_a`'s output as a cell dep (mirroring `TxPoolLimitAncestorCount`). Record `total_tx_size = T`.
2. Submit a transaction that **spends** `tx_a`'s output. `check_and_record_ancestors()` enters the eviction branch (line 603) and calls `remove_entry_and_descendants()` for each evict candidate, triggering `update_stat_for_remove_tx()` per removed entry, correctly setting `self.total_tx_size = T - S_evict`.
3. Lines 218–219 execute: `self.total_tx_size = total_tx_size` (the stale snapshot `T + entry.size`), overwriting the correct value.
4. Pool now reports `total_tx_size ≈ T + entry.size` instead of the correct `T - S_evict + entry.size`.
5. `limit_size()` immediately evicts additional honest transactions; subsequent submissions receive `Reject::Full`.
6. Repeat steps 1–5 to accumulate further inflation until the pool is effectively unusable.
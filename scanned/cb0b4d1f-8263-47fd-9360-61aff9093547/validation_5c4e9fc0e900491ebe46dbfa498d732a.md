Audit Report

## Title
`total_tx_size` / `total_tx_cycles` Overwritten With Stale Pre-Eviction Snapshot in `add_entry` - (File: `tx-pool/src/component/pool_map.rs`)

## Summary

In `PoolMap::add_entry`, the aggregate pool statistics `total_tx_size` and `total_tx_cycles` are snapshotted into local variables before `check_and_record_ancestors` runs. When that function evicts in-pool transactions, it correctly decrements `self.total_tx_size` / `self.total_tx_cycles` via `update_stat_for_remove_tx`. However, lines 218–219 then unconditionally overwrite those correctly-decremented fields with the stale pre-eviction snapshot, permanently inflating the totals by the size/cycles of every evicted transaction. The inflation causes `limit_size` to over-evict legitimate transactions and causes subsequent `add_entry` calls to return `Reject::Full` even when real pool space exists.

## Finding Description

**Root cause — `add_entry` (lines 200–221):**

```rust
// Line 210-211: snapshot computed from pre-eviction state into LOCAL vars
let (total_tx_size, total_tx_cycles) =
    self.updated_stat_for_add_tx(entry.size, entry.cycles)?;

// Line 213: may call remove_entry_and_descendants → remove_entry →
//           update_stat_for_remove_tx, which WRITES to self.total_tx_size / self.total_tx_cycles
evicts = self.check_and_record_ancestors(&mut entry)?;

// ...

// Lines 218-219: OVERWRITES the correctly-decremented fields with the stale snapshot
self.total_tx_size = total_tx_size;
self.total_tx_cycles = total_tx_cycles;
```

The eviction path inside `check_and_record_ancestors` (lines 615–625) calls `remove_entry_and_descendants` → `remove_entry` (line 247: `self.update_stat_for_remove_tx(entry.inner.size, entry.inner.cycles)`), which correctly decrements `self.total_tx_size` and `self.total_tx_cycles`. Lines 218–219 then clobber those correct values.

**Eviction trigger condition (lines 603–625):**
```rust
if ancestors_count.saturating_sub(cell_ref_parents.len()) <= self.max_ancestors_count {
    // evict cell_ref_parents from pool
    while ancestors_count > self.max_ancestors_count {
        let removed = self.remove_entry_and_descendants(next_id);
        ...
    }
}
```
This path fires when a new transaction's ancestor count exceeds `max_ancestors_count` but can be brought within limit by evicting `cell_ref_parents` — transactions that use an ancestor's output as a cell dep.

**Arithmetic:**

| Step | `self.total_tx_size` |
|---|---|
| Initial | 1000 |
| Local snapshot (`updated_stat_for_add_tx`, +200 new tx) | local=1200 |
| After eviction of tx (size=300) via `update_stat_for_remove_tx` | self=700 |
| After line 218 overwrite | **self=1200 (wrong, should be 900)** |

The `recompute_total_stat` fallback in `update_stat_for_remove_tx` (lines 742–756) only triggers on underflow, not on this inflation path, so there is no self-correction.

## Impact Explanation

**High — Vulnerabilities or bad designs which could cause CKB network congestion with few costs.**

1. **Over-eviction via `limit_size`** (`pool.rs` line 298): `while self.pool_map.total_tx_size > self.config.max_tx_pool_size` — an inflated counter causes the loop to evict additional legitimate, fee-paying transactions that would otherwise fit. Each evicted transaction receives `Reject::Full` and is removed from the pool permanently.

2. **False `Reject::Full` on next submission** (`pool_map.rs` lines 716–721): the next call to `updated_stat_for_add_tx` uses the inflated `self.total_tx_size` as its baseline. If the inflated value already exceeds `max_tx_pool_size`, the overflow/size check rejects the incoming transaction with `Reject::Full` even though real pool space exists.

3. **Incorrect RPC reporting** (`service.rs` lines 1089–1090): `tx_pool_info` reads `total_tx_size` and `total_tx_cycles` directly, so callers receive wrong values.

An attacker broadcasting crafted transaction chains via P2P relay can trigger this repeatedly across many nodes simultaneously, causing widespread over-eviction of legitimate transactions and degrading the network's ability to process user transactions.

## Likelihood Explanation

The eviction path requires:
- A chain of transactions where an ancestor's output is used as a cell dep by another in-pool transaction (`cell_ref_parents` non-empty).
- A new transaction whose ancestor count exceeds `max_ancestors_count` (default 25 on mainnet) but can be reduced to ≤25 by evicting the `cell_ref_parents`.

Both conditions are achievable by an unprivileged user via `send_transaction` RPC or P2P relay without any special privileges. The attacker submits a sequence of transactions that builds the required chain structure, then submits a final transaction that triggers the eviction path. This can be repeated to accumulate inflation across multiple `add_entry` calls. The scenario is realistic in a busy mempool with deep transaction chains and shared cell deps, which is a normal CKB cell model usage pattern.

## Recommendation

Remove the early snapshot at lines 210–211. After `check_and_record_ancestors` and all other mutations complete, add the new entry's contribution directly to the post-eviction `self.total_tx_size` / `self.total_tx_cycles`:

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
    // Pre-flight overflow guard (does NOT write to self fields)
    self.updated_stat_for_add_tx(entry.size, entry.cycles)?;

    trace!("pool_map.add_{:?} {}", status, entry.transaction().hash());
    evicts = self.check_and_record_ancestors(&mut entry)?;
    self.record_entry_edges(&entry)?;
    self.insert_entry(&entry, status);
    self.record_entry_descendants(&entry);
    self.track_entry_statics(None, Some(status));

    // Add new entry's contribution to the POST-eviction totals
    self.total_tx_size = self.total_tx_size
        .checked_add(entry.size)
        .ok_or_else(|| Reject::Full(format!(
            "tx-pool total_tx_size {} overflows by add {}",
            self.total_tx_size, entry.size
        )))?;
    self.total_tx_cycles = self.total_tx_cycles
        .checked_add(entry.cycles)
        .ok_or_else(|| Reject::Full(format!(
            "tx-pool total_tx_cycles {} overflows by add {}",
            self.total_tx_cycles, entry.cycles
        )))?;
    Ok((true, evicts))
}
```

The pre-flight call to `updated_stat_for_add_tx` preserves the overflow rejection behavior without capturing a stale snapshot.

## Proof of Concept

**Setup:** `max_ancestors_count = 3`, pool contains `tx_A` (size=100), `tx_B` (size=200, uses `tx_A`'s output as cell dep), `tx_C` (size=150, child of `tx_A`). `total_tx_size = 450`.

**Steps:**
1. Submit `tx_D`: spends `tx_A`'s output (making `tx_B` a `cell_ref_parent`), ancestors = `{tx_A, tx_C}` → `ancestors_count = 3 = max_ancestors_count`, no eviction. Pool: `total_tx_size = 550`.
2. Submit `tx_E` (size=100): spends `tx_C`'s output, cell dep on `tx_A` → `ancestors_count = 4 > 3`, `cell_ref_parents = {tx_B}`, `4 - 1 = 3 ≤ 3` → eviction path fires.

**During `add_entry` for `tx_E`:**
- Line 210: `local_total_tx_size = 550 + 100 = 650`
- `check_and_record_ancestors` evicts `tx_B` (size=200): `self.total_tx_size = 550 - 200 = 350`
- Line 218: `self.total_tx_size = 650` ← **inflated by 200**
- Correct value: `350 + 100 = 450`

**Consequence:** `limit_size` (pool.rs line 298) now sees `total_tx_size = 650` instead of `450`. If `max_tx_pool_size = 600`, it will evict 50+ bytes of legitimate transactions unnecessarily. The next `add_entry` call uses `650` as its baseline, causing premature `Reject::Full`.

A unit test can be written by constructing a `PoolMap` with `max_ancestors_count = 3`, inserting the above transactions, calling `add_entry` for `tx_E`, and asserting `pool_map.total_tx_size == pool_map.entries.iter().map(|e| e.inner.size).sum::<usize>()` — this invariant will fail with the current code.
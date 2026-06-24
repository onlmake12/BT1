Audit Report

## Title
`total_tx_size` / `total_tx_cycles` Overwritten With Stale Pre-Eviction Snapshot in `add_entry` - (File: `tx-pool/src/component/pool_map.rs`)

## Summary

In `PoolMap::add_entry`, lines 210–211 snapshot the post-add totals into local variables before `check_and_record_ancestors` runs. When that function evicts in-pool transactions via `remove_entry` → `update_stat_for_remove_tx`, it correctly decrements `self.total_tx_size` / `self.total_tx_cycles`. Lines 218–219 then unconditionally overwrite those correctly-decremented fields with the stale pre-eviction snapshot, permanently inflating the pool size/cycle counters by the size/cycles of every evicted transaction. This causes `limit_size` to over-evict legitimate transactions and causes subsequent `add_entry` calls to return `Reject::Full` even when real pool space exists.

## Finding Description

**Root cause — `add_entry` (lines 200–221):**

`updated_stat_for_add_tx` at lines 710–729 is a `&self` (immutable) method. It computes `self.total_tx_size + entry.size` and `self.total_tx_cycles + entry.cycles` and returns them as local variables without writing to `self`. [1](#0-0) 

Lines 210–211 capture this pre-eviction snapshot: [2](#0-1) 

Line 213 calls `check_and_record_ancestors`, which at lines 616–625 may call `remove_entry_and_descendants` → `remove_entry` → `update_stat_for_remove_tx`. `update_stat_for_remove_tx` at lines 738–740 **does** write to `self.total_tx_size` and `self.total_tx_cycles`, correctly decrementing them for each evicted transaction. [3](#0-2) [4](#0-3) 

Lines 218–219 then unconditionally overwrite the correctly-decremented `self` fields with the stale local snapshot: [5](#0-4) 

The eviction path fires at lines 603–625 when `ancestors_count - cell_ref_parents.len() <= max_ancestors_count` but `ancestors_count > max_ancestors_count`. [6](#0-5) 

**Arithmetic (concrete example):**

| Step | `self.total_tx_size` |
|---|---|
| Initial | 550 |
| Local snapshot (`updated_stat_for_add_tx`, +100 new tx) | local=650 |
| After eviction of tx_B (size=200) via `update_stat_for_remove_tx` | self=350 |
| After line 218 overwrite | **self=650 (wrong, should be 450)** |

The `recompute_total_stat` fallback in `update_stat_for_remove_tx` (lines 742–756) only triggers on underflow, not on this inflation path, so there is no self-correction. [7](#0-6) 

## Impact Explanation

**High — Vulnerabilities or bad designs which could cause CKB network congestion with few costs.**

1. **Over-eviction via `limit_size`** (pool.rs line 298): `while self.pool_map.total_tx_size > self.config.max_tx_pool_size` — an inflated counter causes the loop to evict additional legitimate, fee-paying transactions that would otherwise fit. [8](#0-7) 

2. **False `Reject::Full` on next submission** (pool_map.rs lines 716–721): the next call to `updated_stat_for_add_tx` uses the inflated `self.total_tx_size` as its baseline, causing premature rejection even when real pool space exists. [9](#0-8) 

3. **Incorrect RPC reporting** (service.rs lines 1089–1090): `tx_pool_info` reads `total_tx_size` and `total_tx_cycles` directly, so callers receive wrong values. [10](#0-9) 

An attacker broadcasting crafted transaction chains via P2P relay can trigger this repeatedly across many nodes simultaneously, causing widespread over-eviction of legitimate transactions and degrading the network's ability to process user transactions.

## Likelihood Explanation

The eviction path requires a chain where an ancestor's output is used as a cell dep by another in-pool transaction (`cell_ref_parents` non-empty), and a new transaction whose ancestor count exceeds `max_ancestors_count` (default 25 on mainnet) but can be reduced to ≤25 by evicting those `cell_ref_parents`. Both conditions are achievable by an unprivileged user via `send_transaction` RPC or P2P relay without any special privileges. The scenario is realistic in a busy mempool with deep transaction chains and shared cell deps, which is a normal CKB cell model usage pattern. The attack can be repeated to accumulate inflation across multiple `add_entry` calls.

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

## Proof of Concept

**Setup:** `max_ancestors_count = 3`, pool contains `tx_A` (size=100), `tx_B` (size=200, uses `tx_A`'s output as cell dep), `tx_C` (size=150, child of `tx_A`). `total_tx_size = 450`.

**Steps:**
1. Submit `tx_D`: spends `tx_A`'s output, ancestors = `{tx_A, tx_C}` → `ancestors_count = 3 = max_ancestors_count`, no eviction. Pool: `total_tx_size = 550`.
2. Submit `tx_E` (size=100): spends `tx_C`'s output, cell dep on `tx_A` → `ancestors_count = 4 > 3`, `cell_ref_parents = {tx_B}`, `4 - 1 = 3 ≤ 3` → eviction path fires.

**During `add_entry` for `tx_E`:**
- Line 210: `local_total_tx_size = 550 + 100 = 650`
- `check_and_record_ancestors` evicts `tx_B` (size=200): `self.total_tx_size = 350`
- Line 218: `self.total_tx_size = 650` ← inflated by 200
- Correct value: `350 + 100 = 450`

**Invariant test:** Construct a `PoolMap` with `max_ancestors_count = 3`, insert the above transactions, call `add_entry` for `tx_E`, and assert:
```rust
assert_eq!(
    pool_map.total_tx_size,
    pool_map.entries.iter().map(|e| e.inner.size).sum::<usize>()
);
```
This invariant will fail with the current code, confirming the inflation.

### Citations

**File:** tx-pool/src/component/pool_map.rs (L210-211)
```rust
        let (total_tx_size, total_tx_cycles) =
            self.updated_stat_for_add_tx(entry.size, entry.cycles)?;
```

**File:** tx-pool/src/component/pool_map.rs (L218-219)
```rust
        self.total_tx_size = total_tx_size;
        self.total_tx_cycles = total_tx_cycles;
```

**File:** tx-pool/src/component/pool_map.rs (L247-247)
```rust
            self.update_stat_for_remove_tx(entry.inner.size, entry.inner.cycles);
```

**File:** tx-pool/src/component/pool_map.rs (L603-625)
```rust
        if ancestors_count.saturating_sub(cell_ref_parents.len()) <= self.max_ancestors_count {
            // if ancestors count exceed limitation,
            // try to evict some conflicted transactions due to ref cells

            // sort them to find out the transactions with lowest fees
            let evict_candidates: Vec<ProposalShortId> = self
                .entries
                .iter_by_evict_key()
                .filter(move |entry| cell_ref_parents.contains(&entry.id))
                .map(|x| x.id.clone())
                .collect();

            let mut iter = evict_candidates.iter();
            while ancestors_count > self.max_ancestors_count {
                if let Some(next_id) = iter.next() {
                    let removed = self.remove_entry_and_descendants(next_id);
                    ancestors_count = ancestors_count.saturating_sub(1);
                    parents.remove(next_id);
                    evicted.extend(removed);
                } else {
                    break;
                }
            }
```

**File:** tx-pool/src/component/pool_map.rs (L711-729)
```rust
    fn updated_stat_for_add_tx(
        &self,
        tx_size: usize,
        cycles: Cycle,
    ) -> Result<(usize, Cycle), Reject> {
        let total_tx_size = self.total_tx_size.checked_add(tx_size).ok_or_else(|| {
            Reject::Full(format!(
                "tx-pool total_tx_size {} overflows by add {}",
                self.total_tx_size, tx_size
            ))
        })?;
        let total_tx_cycles = self.total_tx_cycles.checked_add(cycles).ok_or_else(|| {
            Reject::Full(format!(
                "tx-pool total_tx_cycles {} overflows by add {}",
                self.total_tx_cycles, cycles
            ))
        })?;
        Ok((total_tx_size, total_tx_cycles))
    }
```

**File:** tx-pool/src/component/pool_map.rs (L733-741)
```rust
    fn update_stat_for_remove_tx(&mut self, tx_size: usize, cycles: Cycle) {
        match (
            self.total_tx_size.checked_sub(tx_size),
            self.total_tx_cycles.checked_sub(cycles),
        ) {
            (Some(total_tx_size), Some(total_tx_cycles)) => {
                self.total_tx_size = total_tx_size;
                self.total_tx_cycles = total_tx_cycles;
            }
```

**File:** tx-pool/src/component/pool_map.rs (L742-756)
```rust
            _ => {
                if let Some((total_tx_size, total_tx_cycles)) = self.recompute_total_stat() {
                    error!(
                        "tx-pool total stats underflowed when removing size {} cycles {}, recomputed size {} cycles {}",
                        tx_size, cycles, total_tx_size, total_tx_cycles
                    );
                    self.total_tx_size = total_tx_size;
                    self.total_tx_cycles = total_tx_cycles;
                } else {
                    error!(
                        "tx-pool total stats underflowed when removing size {} cycles {}, and recomputing overflowed",
                        tx_size, cycles
                    );
                }
            }
```

**File:** tx-pool/src/pool.rs (L298-307)
```rust
        while self.pool_map.total_tx_size > self.config.max_tx_pool_size {
            let next_evict_entry = || {
                self.pool_map
                    .next_evict_entry(Status::Pending)
                    .or_else(|| self.pool_map.next_evict_entry(Status::Gap))
                    .or_else(|| self.pool_map.next_evict_entry(Status::Proposed))
            };

            if let Some(id) = next_evict_entry() {
                let removed = self.pool_map.remove_entry_and_descendants(&id);
```

**File:** tx-pool/src/service.rs (L1089-1090)
```rust
            total_tx_size: tx_pool.pool_map.total_tx_size,
            total_tx_cycles: tx_pool.pool_map.total_tx_cycles,
```

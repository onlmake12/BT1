Audit Report

## Title
`total_tx_size` / `total_tx_cycles` Inflated by Stale Write-Back After Ancestor-Eviction in `add_entry` — (File: `tx-pool/src/component/pool_map.rs`)

## Summary

In `PoolMap::add_entry`, the new pool totals are computed into local variables before `check_and_record_ancestors` runs. When that function evicts transactions via `remove_entry_and_descendants` → `update_stat_for_remove_tx`, it correctly decrements `self.total_tx_size` / `self.total_tx_cycles`. However, lines 218–219 then unconditionally overwrite those fields with the stale pre-eviction snapshot, silently erasing every decrement. The result is a permanently inflated `total_tx_size`, which drives `limit_size` to evict legitimate transactions and reject new submissions with `Reject::Full` even when actual pool occupancy is below the configured limit.

## Finding Description

The exact sequence in `add_entry` (lines 200–221):

```
// Step 1 — snapshot pre-eviction totals + new tx into LOCALS
let (total_tx_size, total_tx_cycles) =
    self.updated_stat_for_add_tx(entry.size, entry.cycles)?;   // L210-211
    // captures: self.total_tx_size + entry.size

// Step 2 — may evict; each removal calls update_stat_for_remove_tx
//           which DECREMENTS self.total_tx_size / self.total_tx_cycles
evicts = self.check_and_record_ancestors(&mut entry)?;          // L213

// Step 3 — OVERWRITE self fields with the stale snapshot from Step 1
self.total_tx_size  = total_tx_size;                            // L218
self.total_tx_cycles = total_tx_cycles;                         // L219
``` [1](#0-0) 

`updated_stat_for_add_tx` reads `self.total_tx_size` at call time and returns `self.total_tx_size + entry.size` as a local value. [2](#0-1) 

`check_and_record_ancestors` enters the eviction branch when `ancestors_count > max_ancestors_count` AND `ancestors_count - cell_ref_parents.len() <= max_ancestors_count`, calling `remove_entry_and_descendants` for each candidate. [3](#0-2) 

`update_stat_for_remove_tx` correctly decrements `self.total_tx_size` and `self.total_tx_cycles` in-place. [4](#0-3) 

But the write-back at L218–219 restores the pre-eviction snapshot, so the net accounting after one eviction cycle is:

```
self.total_tx_size = original_total + new_tx_size
                     (evicted sizes are NOT subtracted)
```

Each triggering submission inflates `total_tx_size` by the aggregate size of all evicted transactions.

## Impact Explanation

`limit_size` in `pool.rs` drives all pool-size enforcement directly from `pool_map.total_tx_size`: [5](#0-4) 

With inflated `total_tx_size`:
- The pool evicts legitimate, higher-fee transactions unnecessarily on every subsequent `add_entry` call.
- New submissions are rejected with `Reject::Full` even though actual byte occupancy is below `max_tx_pool_size`.
- Inflation accumulates monotonically across repeated triggering submissions until the pool is effectively frozen.

This matches the allowed High impact: **"Vulnerabilities or bad designs which could cause CKB network congestion with few costs"** (10001–15000 points). The attacker pays only normal transaction fees; no privileged access is required.

## Likelihood Explanation

The eviction branch in `check_and_record_ancestors` requires:
1. A new transaction whose ancestor count exceeds `max_ancestors_count` (default 25).
2. At least one ancestor that is also a `cell_ref_parent` (i.e., an existing pool transaction that references, as a cell-dep, a live cell that the new transaction consumes as an input).

Both conditions are reachable by any unprivileged P2P or RPC transaction sender by constructing a transaction graph where a chain of 25 pool transactions includes one that cell-dep-references a live cell, and the attacker's new transaction spends that cell. This is a crafted but realistic graph requiring no key material, no majority hashpower, and no privileged access. The attack is repeatable with fresh transactions.

## Recommendation

Move the total-update to after all evictions complete, so it reads the already-decremented `self` fields:

```rust
// Remove the pre-eviction snapshot entirely.
// After check_and_record_ancestors returns, self.total_tx_size already
// reflects all evictions; just add the new entry's contribution.
evicts = self.check_and_record_ancestors(&mut entry)?;
self.record_entry_edges(&entry)?;
self.insert_entry(&entry, status);
self.record_entry_descendants(&entry);
self.track_entry_statics(None, Some(status));
// Post-eviction delta — reads the correct decremented self fields:
self.total_tx_size = self.total_tx_size
    .checked_add(entry.size)
    .expect("size overflow");
self.total_tx_cycles = self.total_tx_cycles
    .checked_add(entry.cycles)
    .expect("cycles overflow");
```

The overflow guard previously provided by `updated_stat_for_add_tx` should be preserved; it can be called after eviction or replaced with `checked_add` as shown above.

## Proof of Concept

**Setup:**
- `max_ancestors_count = 25`
- Pool contains T1→T2→…→T25 (chain of 25 txs). T25 also references live cell `O_base` as a cell-dep.

**Attack step:**
1. Submit `T_attack` with `inputs = [O_base]` (consuming it) and `cell_deps = [outputs of T1…T25]`.
   - `ancestors_count = 26 > 25` → eviction branch entered.
   - `cell_ref_parents = {T25}` (T25 cell-dep-references `O_base` which `T_attack` consumes).
   - `26 - 1 = 25 <= 25` → condition satisfied.
   - T25 is evicted: `update_stat_for_remove_tx(T25.size, T25.cycles)` decrements `self.total_tx_size`.
   - `T_attack` is inserted.
   - L218–219 restore `total_tx_size = original_total + T_attack.size`, adding back T25's size.
   - **Net inflation per round = T25.size.**

2. Repeat with fresh analogous transaction graphs. Each round inflates `total_tx_size` by the evicted transaction's size.

3. Once `total_tx_size` exceeds `max_tx_pool_size`, `limit_size` evicts all pending transactions and every new submission is rejected with `Reject::Full`.

A unit test can assert `pool_map.total_tx_size == recompute_total_stat()` after each `add_entry` call that triggers eviction; the assertion will fail on the current code, confirming the bug.

### Citations

**File:** tx-pool/src/component/pool_map.rs (L210-219)
```rust
        let (total_tx_size, total_tx_cycles) =
            self.updated_stat_for_add_tx(entry.size, entry.cycles)?;
        trace!("pool_map.add_{:?} {}", status, entry.transaction().hash());
        evicts = self.check_and_record_ancestors(&mut entry)?;
        self.record_entry_edges(&entry)?;
        self.insert_entry(&entry, status);
        self.record_entry_descendants(&entry);
        self.track_entry_statics(None, Some(status));
        self.total_tx_size = total_tx_size;
        self.total_tx_cycles = total_tx_cycles;
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

**File:** tx-pool/src/component/pool_map.rs (L711-728)
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

**File:** tx-pool/src/pool.rs (L298-326)
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
                for entry in removed {
                    let tx_hash = entry.transaction().hash();
                    debug!(
                        "Removed by size limit {} timestamp({})",
                        tx_hash, entry.timestamp
                    );
                    let reject = Reject::Full(format!(
                        "the fee_rate for this transaction is: {}",
                        entry.fee_rate()
                    ));
                    if let Some(short_id) = current_entry_id
                        && entry.proposal_short_id() == *short_id
                    {
                        ret = Some(reject.clone());
                    }
                    callbacks.call_reject(self, &entry, reject);
                }
            }
        }
```

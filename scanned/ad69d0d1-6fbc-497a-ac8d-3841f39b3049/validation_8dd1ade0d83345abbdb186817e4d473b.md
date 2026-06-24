Audit Report

## Title
Stale Pre-Eviction Snapshot Overwrites Live `total_tx_size`/`total_tx_cycles` Counters in `add_entry` — (File: `tx-pool/src/component/pool_map.rs`)

## Summary
In `PoolMap::add_entry`, new pool-size totals are computed into local variables via `updated_stat_for_add_tx` (which takes `&self` and does not mutate) before a conditional eviction step runs. If `check_and_record_ancestors` evicts transactions — correctly decrementing `self.total_tx_size`/`self.total_tx_cycles` in-place via `update_stat_for_remove_tx` — the stale pre-eviction locals are then unconditionally written back at lines 218–219, silently cancelling every decrement. Each such call inflates the counters by the aggregate size/cycles of all evicted entries, eventually causing `limit_size` to fire spuriously and evict legitimate transactions.

## Finding Description
The exact sequence in `add_entry` (lines 200–221):

1. **Line 210–211**: `updated_stat_for_add_tx` takes `&self` (immutable), reads `self.total_tx_size`, and returns `self.total_tx_size + entry.size` as a local — it does **not** write to `self`. [1](#0-0) 

2. **Line 213**: `check_and_record_ancestors` takes `&mut self`. When `ancestors_count > max_ancestors_count` and `ancestors_count - cell_ref_parents.len() <= max_ancestors_count` (line 603), it enters the eviction loop (lines 615–625), calling `remove_entry_and_descendants` for each candidate. This chain reaches `update_stat_for_remove_tx` (lines 733–758), which **mutates** `self.total_tx_size` and `self.total_tx_cycles` in-place. [2](#0-1) [3](#0-2) 

3. **Lines 218–219**: The stale local snapshot is unconditionally written back, overwriting the correctly-decremented live values. [4](#0-3) 

Concrete arithmetic: if `self.total_tx_size = 100`, new tx size = 10, evicted tx size = 20 — after eviction `self.total_tx_size` is correctly 80, but line 218 writes back 110 (the stale snapshot), leaving the counter 30 bytes inflated instead of the correct 90.

`updated_stat_for_add_tx` is confirmed read-only (`&self`): [5](#0-4) 

`update_stat_for_remove_tx` is confirmed to mutate `self`: [6](#0-5) 

## Impact Explanation
`total_tx_size` is the sole guard in `limit_size` (pool.rs line 298): [7](#0-6) 

An inflated counter causes `limit_size` to believe the pool is over capacity, triggering cascading spurious evictions of legitimate fee-paying transactions and returning `Reject::Full` to all new submissions. Repeated exploitation drives the counter arbitrarily high with minimal real pool occupancy, effectively disabling the mempool on the targeted node. This matches the **High** impact: *"Vulnerabilities or bad designs which could cause CKB network congestion with few costs."*

## Likelihood Explanation
The eviction branch in `check_and_record_ancestors` is reachable by any unprivileged transaction sender. The attacker needs only to:
- Submit a base transaction `T0` whose output is used as a cell dep by several pool transactions (`T1…Tk`).
- Submit `Tnew` spending an output of `T0`, making `T1…Tk` cell-ref ancestors, pushing `ancestors_count` just over `max_ancestors_count`.

No privileged access, no majority hashpower, and no external dependency is required. The entry path is the standard `send_transaction` RPC / P2P relay path. The attack is repeatable with fresh transactions to accumulate unbounded inflation. [8](#0-7) 

## Recommendation
Remove the pre-eviction snapshot pattern entirely. Instead, validate capacity with a read-only check (to preserve the overflow/rejection guard), let evictions run, then apply the addition **after** all removals have already decremented the counters:

```rust
// Validate capacity (read-only, preserves overflow rejection)
self.updated_stat_for_add_tx(entry.size, entry.cycles)?;
trace!("pool_map.add_{:?} {}", status, entry.transaction().hash());
evicts = self.check_and_record_ancestors(&mut entry)?;
self.record_entry_edges(&entry)?;
self.insert_entry(&entry, status);
self.record_entry_descendants(&entry);
self.track_entry_statics(None, Some(status));
// Apply addition AFTER evictions have already decremented the counters
self.total_tx_size = self.total_tx_size.saturating_add(entry.size);
self.total_tx_cycles = self.total_tx_cycles.saturating_add(entry.cycles);
```

This ensures the final counters reflect: `(pre-add value) - (evicted sizes) + (new tx size)`.

## Proof of Concept
**Setup:** `max_ancestors_count = 25`, `max_tx_pool_size = 1_000_000` bytes.

1. Submit base tx `T0` creating output `O`.
2. Submit 24 txs `T1…T24`, each referencing `O` as a cell dep (~1 000 bytes each). Pool: 25 entries, `total_tx_size ≈ 25_000`.
3. Submit `Tnew` (1 000 bytes) spending an output of `T0`. Ancestor set includes `T1…T24` via cell-dep linkage → `ancestors_count = 26 > 25`. `cell_ref_parents = {T1…T24}`, so `26 - 24 = 2 ≤ 25` — eviction branch taken.
4. `check_and_record_ancestors` evicts e.g. `T1`, `T2` (2 000 bytes). `update_stat_for_remove_tx` correctly sets `self.total_tx_size = 23_000`.
5. Line 218 writes back stale snapshot: `self.total_tx_size = 26_000` (should be `24_000`). Inflation: **+2 000 bytes per call**.
6. Repeat ~490 times. `total_tx_size` reaches `≈ 1_000_000` while actual pool is nearly empty. `limit_size` fires on every subsequent `add_entry`, evicting legitimate transactions and returning `Reject::Full` to all new submissions. [9](#0-8) [10](#0-9)

### Citations

**File:** tx-pool/src/component/pool_map.rs (L200-221)
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
        Ok((true, evicts))
    }
```

**File:** tx-pool/src/component/pool_map.rs (L595-628)
```rust
        let mut ancestors_count = ancestors.len() + 1;
        let mut evicted = Default::default();

        if ancestors_count <= self.max_ancestors_count {
            self._record_ancestors(entry, ancestors, parents);
            return Ok(evicted);
        }

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
        } else {
            return Err(Reject::ExceededMaximumAncestorsCount);
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

**File:** tx-pool/src/component/pool_map.rs (L733-758)
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
        }
    }
```

**File:** tx-pool/src/pool.rs (L292-329)
```rust
    pub(crate) fn limit_size(
        &mut self,
        callbacks: &Callbacks,
        current_entry_id: Option<&ProposalShortId>,
    ) -> Option<Reject> {
        let mut ret = None;
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
        self.pool_map.entries.shrink_to_fit();
        ret
    }
```

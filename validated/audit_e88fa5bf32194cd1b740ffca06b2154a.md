Audit Report

## Title
`PoolMap::add_entry` Overwrites `total_tx_size`/`total_tx_cycles` With Pre-Eviction Snapshot, Permanently Inflating Pool Accounting — (File: tx-pool/src/component/pool_map.rs)

## Summary

In `PoolMap::add_entry`, `updated_stat_for_add_tx` captures a snapshot of `self.total_tx_size + new_entry_size` before `check_and_record_ancestors` runs. That function may evict existing entries via `remove_entry_and_descendants` → `update_stat_for_remove_tx`, which decrements `self.total_tx_size` in-place. The pre-eviction snapshot is then unconditionally written back at lines 218–219, silently discarding those decrements. Each eviction event permanently inflates `total_tx_size` by the evicted transaction's size, and the inflation is cumulative.

## Finding Description

In `add_entry` (lines 200–221 of `tx-pool/src/component/pool_map.rs`):

```rust
// Step A: snapshot = old_total + new_entry, BEFORE any evictions
let (total_tx_size, total_tx_cycles) =
    self.updated_stat_for_add_tx(entry.size, entry.cycles)?;  // lines 210-211

// Step B: may call remove_entry_and_descendants → update_stat_for_remove_tx
//         → self.total_tx_size -= evicted_size  (in-place mutation)
evicts = self.check_and_record_ancestors(&mut entry)?;         // line 213

// Step C: OVERWRITES self.total_tx_size with the pre-eviction snapshot
self.total_tx_size = total_tx_size;                            // line 218
self.total_tx_cycles = total_tx_cycles;                        // line 219
```

`updated_stat_for_add_tx` (lines 711–729) is a pure read: it returns `self.total_tx_size + tx_size` without modifying any state. [1](#0-0) 

`check_and_record_ancestors` (lines 603–625) enters the eviction branch when `ancestors_count.saturating_sub(cell_ref_parents.len()) <= self.max_ancestors_count` and calls `remove_entry_and_descendants` for each cell-ref parent to be evicted. [2](#0-1) 

`update_stat_for_remove_tx` (lines 733–741) decrements `self.total_tx_size` in-place via `checked_sub`. [3](#0-2) 

After step C, `self.total_tx_size` equals `(pre-eviction total + new_entry_size)` instead of the correct `(post-eviction total + new_entry_size)`. The evicted transactions' sizes remain permanently counted. The same logic applies to `total_tx_cycles`. [4](#0-3) 

## Impact Explanation

`total_tx_size` is the sole gate for two critical pool-admission decisions:

1. **`limit_size`** (pool.rs lines 298–326) evicts transactions while `total_tx_size > max_tx_pool_size`. An inflated counter causes the loop to evict legitimate, fee-paying transactions that would otherwise fit. [5](#0-4) 

2. **`updated_stat_for_add_tx`** rejects new submissions with `Reject::Full` when the pool appears full. An inflated counter causes valid transactions to be rejected even when the pool has real capacity. [6](#0-5) 

The inflation is permanent and cumulative. Repeated triggering drives `total_tx_size` arbitrarily high, eventually making the pool reject all new submissions with `Reject::Full` while the pool is actually near-empty. This matches the **High** impact: *"Vulnerabilities or bad designs which could cause CKB network congestion with few costs."*

## Likelihood Explanation

The eviction branch in `check_and_record_ancestors` is reachable by any unprivileged RPC caller via `send_transaction`. The attacker needs only to submit a transaction that uses an existing pool entry's output as a **cell dep** (making it a `cell_ref_parent`), then submit a transaction whose ancestor count exceeds `max_ancestors_count` but whose ancestor count minus the cell-ref parents falls within the limit. No privileged access, no key material, and no majority hashpower is required. The attack is repeatable with low cost per iteration.

## Recommendation

Compute the new totals **after** `check_and_record_ancestors` completes, so that any in-place decrements from evictions are already reflected before the snapshot is taken:

```rust
evicts = self.check_and_record_ancestors(&mut entry)?;
self.record_entry_edges(&entry)?;
self.insert_entry(&entry, status);
self.record_entry_descendants(&entry);
self.track_entry_statics(None, Some(status));
// Compute totals AFTER evictions have already updated self.total_tx_size
let (total_tx_size, total_tx_cycles) =
    self.updated_stat_for_add_tx(entry.size, entry.cycles)?;
self.total_tx_size = total_tx_size;
self.total_tx_cycles = total_tx_cycles;
```

Alternatively, use `recompute_total_stat` as a post-insertion consistency check, or simply read `self.total_tx_size` directly after evictions rather than caching a pre-eviction snapshot. [7](#0-6) 

## Proof of Concept

**Setup:** `max_ancestors_count = 2`, `max_tx_pool_size = 10_000`
- Tx-A (size=5000) is in the pool.
- Tx-B (size=100) uses Tx-A's output as a cell dep (`cell_ref_parent`). Pool: `total_tx_size = 5100`.

**Attack:** Submit Tx-C (size=200) with `ancestors_count = 3 > max_ancestors_count = 2`, but `cell_ref_parents = {Tx-A}`, so `3 - 1 = 2 <= max_ancestors_count`.

**Execution in `add_entry` for Tx-C:**
1. `updated_stat_for_add_tx(200, ...)` → snapshot `total_tx_size = 5100 + 200 = 5300`
2. `check_and_record_ancestors` evicts Tx-A (size=5000): `self.total_tx_size = 5100 - 5000 = 100`
3. `self.total_tx_size = 5300` ← overwrites; Tx-A's 5000 bytes are re-added

**Result:** Pool contains Tx-B (100) + Tx-C (200) = 300 bytes of real data, but `total_tx_size = 5300`. Repeating this pattern drives `total_tx_size` past `max_tx_pool_size`, causing `limit_size` to evict all remaining legitimate transactions and `updated_stat_for_add_tx` to reject all new submissions with `Reject::Full`. [8](#0-7)

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

**File:** tx-pool/src/component/pool_map.rs (L698-708)
```rust
    fn recompute_total_stat(&self) -> Option<(usize, Cycle)> {
        self.entries.iter().try_fold(
            (0usize, 0 as Cycle),
            |(total_size, total_cycles), (_, entry)| {
                Some((
                    total_size.checked_add(entry.inner.size)?,
                    total_cycles.checked_add(entry.inner.cycles)?,
                ))
            },
        )
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

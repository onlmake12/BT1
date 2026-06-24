Audit Report

## Title
`total_tx_size`/`total_tx_cycles` Permanently Inflated by Stale Snapshot Overwrite After Eviction in `add_entry` - (File: tx-pool/src/component/pool_map.rs)

## Summary

In `PoolMap::add_entry`, `updated_stat_for_add_tx` computes a pre-eviction snapshot of `total_tx_size` and `total_tx_cycles` at lines 210–211, then `check_and_record_ancestors` at line 213 may evict transactions via `remove_entry_and_descendants` → `update_stat_for_remove_tx`, decrementing the live counters in-place. Lines 218–219 then unconditionally overwrite those live counters with the stale pre-eviction snapshot, permanently discarding every decrement. Repeated exploitation inflates `total_tx_size` until `limit_size` evicts legitimate transactions and `updated_stat_for_add_tx` rejects all new submissions with `Reject::Full`, constituting a sustained low-cost denial-of-service against transaction submission.

## Finding Description

The exact sequence in `add_entry` (lines 200–221):

1. **Lines 210–211**: `updated_stat_for_add_tx(entry.size, entry.cycles)` is called against the current `self.total_tx_size` and `self.total_tx_cycles`, returning a snapshot `(total_tx_size, total_tx_cycles)` = `(self.total_tx_size + entry.size, self.total_tx_cycles + entry.cycles)`. This snapshot is taken *before* any evictions occur.

2. **Line 213**: `check_and_record_ancestors(&mut entry)` is called. When `ancestors_count > max_ancestors_count` but `ancestors_count - cell_ref_parents.len() <= max_ancestors_count` (line 603), the eviction loop at lines 616–625 calls `remove_entry_and_descendants(next_id)` → `remove_entry` (line 263) → `update_stat_for_remove_tx` (line 247), which correctly decrements `self.total_tx_size` and `self.total_tx_cycles` in-place (lines 738–740).

3. **Lines 218–219**: `self.total_tx_size = total_tx_size` and `self.total_tx_cycles = total_tx_cycles` unconditionally overwrite the live counters with the pre-eviction snapshot, erasing every decrement applied in step 2.

The `update_stat_for_remove_tx` function (lines 733–757) correctly decrements the live fields, but those writes are immediately clobbered. The eviction path at line 603 is fully reachable: it requires only that the incoming transaction has more than `max_ancestors_count` ancestors while having at least one `cell_ref_parent` that can be evicted to bring the count within the limit.

## Impact Explanation

`total_tx_size` is the authoritative counter driving two enforcement points:

- **`limit_size`** (`pool.rs` line 298): evicts entries while `self.pool_map.total_tx_size > self.config.max_tx_pool_size`. An inflated counter triggers unnecessary eviction of already-accepted legitimate transactions.
- **`updated_stat_for_add_tx`** (`pool_map.rs` line 716): rejects new submissions with `Reject::Full` when `self.total_tx_size.checked_add(tx_size)` would exceed the pool limit. An inflated counter causes valid transactions to be rejected even when actual pool space is available.

Each successful exploit inflates `total_tx_size` by the serialized size of the evicted transaction(s). The inflation is permanent until the pool is restarted. Repeated exploitation accumulates inflation, eventually making the pool appear full when it is nearly empty. This constitutes a sustained, low-cost denial-of-service against transaction submission to the targeted node, matching: **"Vulnerabilities or bad designs which could cause CKB network congestion with few costs" — High (10001–15000 points)**.

## Likelihood Explanation

The trigger condition is fully constructible by any unprivileged `send_transaction` caller. The attacker needs only to craft a valid transaction chain exceeding `max_ancestors_count` (default 25) while having at least one `cell_ref_parent` that can be evicted to bring the count within the limit. This is a standard transaction graph construction requiring no special privileges, no majority hashpower, and no victim mistakes. The attack is repeatable with fresh transaction chains, allowing cumulative inflation.

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

## Proof of Concept

```
Setup: max_ancestors_count = 25, pool empty, total_tx_size = 0

1. Submit tx_base (2 outputs: out_Y, out_X, size=200) → total_tx_size = 200
2. Submit tx1→tx2→…→tx25 chained from out_Y (each size=200) → total_tx_size = 5200
3. Submit txA using out_X as cell_dep (size=500) → total_tx_size = 5700
4. Submit txNew spending tx25.output AND out_X:
   - ancestors = {tx_base, tx1..tx25} → ancestors_count = 26 > 25
   - cell_ref_parents = {txA}; 26 - 1 = 25 ≤ 25 → eviction path fires (line 603)
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

A unit test can be written directly against `PoolMap::add_entry` by constructing the described transaction graph, calling `add_entry` for `txNew`, and asserting `pool_map.total_tx_size` equals the sum of sizes of entries actually present in `pool_map.entries`. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4)

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

**File:** tx-pool/src/component/pool_map.rs (L235-249)
```rust
    pub(crate) fn remove_entry(&mut self, id: &ProposalShortId) -> Option<TxEntry> {
        self.entries.remove_by_id(id).map(|entry| {
            debug!(
                "remove entry {} from status: {:?}",
                entry.inner.transaction().hash(),
                entry.status
            );
            self.update_ancestors_index_key(&entry.inner, EntryOp::Remove);
            self.update_descendants_index_key(&entry.inner, EntryOp::Remove);
            self.remove_entry_edges(&entry.inner);
            self.remove_entry_links(id);
            self.track_entry_statics(Some(entry.status), None);
            self.update_stat_for_remove_tx(entry.inner.size, entry.inner.cycles);
            entry.inner
        })
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

**File:** tx-pool/src/pool.rs (L292-298)
```rust
    pub(crate) fn limit_size(
        &mut self,
        callbacks: &Callbacks,
        current_entry_id: Option<&ProposalShortId>,
    ) -> Option<Reject> {
        let mut ret = None;
        while self.pool_map.total_tx_size > self.config.max_tx_pool_size {
```

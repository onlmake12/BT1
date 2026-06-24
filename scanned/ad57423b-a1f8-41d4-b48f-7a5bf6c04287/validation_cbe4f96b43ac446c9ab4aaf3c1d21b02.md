The code confirms the claim. Let me verify the full call chain for `remove_entry_and_descendants` to ensure `update_stat_for_remove_tx` is indeed called during eviction.

The code fully confirms the claim. The call chain is:

- `add_entry` line 210-211: captures `total_tx_size = self.total_tx_size + entry.size` into locals
- `check_and_record_ancestors` line 213 → `remove_entry_and_descendants` line 618 → `remove_entry` line 263 → `update_stat_for_remove_tx` line 247: correctly decrements `self.total_tx_size`
- `add_entry` lines 218-219: unconditionally overwrites `self.total_tx_size` with the stale pre-eviction local

The bug is real and confirmed by the actual code.

---

Audit Report

## Title
Stale Pre-Computed `total_tx_size`/`total_tx_cycles` Overwritten After Eviction in `add_entry` Corrupts Pool Accounting - (File: `tx-pool/src/component/pool_map.rs`)

## Summary
In `PoolMap::add_entry`, new pool totals are computed into local variables before `check_and_record_ancestors` runs. That function can evict existing entries via `remove_entry_and_descendants` → `remove_entry` → `update_stat_for_remove_tx`, which correctly decrements `self.total_tx_size`/`self.total_tx_cycles`. However, the stale pre-eviction locals are then unconditionally written back to `self.total_tx_size`/`self.total_tx_cycles`, silently discarding the eviction accounting. The result is permanent, cumulative inflation of the pool size counters by the sizes/cycles of every evicted entry.

## Finding Description
`PoolMap::add_entry` (lines 200–221) follows this exact sequence:

```rust
// Line 210-211: captures self.total_tx_size + entry.size into a local BEFORE any evictions
let (total_tx_size, total_tx_cycles) =
    self.updated_stat_for_add_tx(entry.size, entry.cycles)?;

// Line 213: may evict entries; each eviction calls update_stat_for_remove_tx,
// which correctly subtracts from self.total_tx_size / self.total_tx_cycles
evicts = self.check_and_record_ancestors(&mut entry)?;

// Lines 218-219: OVERWRITES the correctly-updated self.total_tx_size with the stale value
self.total_tx_size = total_tx_size;
self.total_tx_cycles = total_tx_cycles;
```

`updated_stat_for_add_tx` (lines 711–729) is a `&self` method that simply returns `self.total_tx_size + tx_size` — it does not mutate state. The eviction path inside `check_and_record_ancestors` (lines 603–625) calls `remove_entry_and_descendants` (line 618), which calls `remove_entry` (line 263), which calls `update_stat_for_remove_tx` (line 247) — correctly decrementing `self.total_tx_size`. After `check_and_record_ancestors` returns, the correctly-updated `self.total_tx_size` is immediately overwritten with the stale local. Net effect: `self.total_tx_size = original + entry.size` instead of the correct `original − evicted_sizes + entry.size`. The inflation equals the sum of sizes of all evicted entries and is permanent with no self-correcting mechanism.

## Impact Explanation
`total_tx_size` is the authoritative pool-size counter used by `limit_size` (pool.rs line 298): `while self.pool_map.total_tx_size > self.config.max_tx_pool_size`. An inflated counter causes `limit_size` to evict legitimate transactions even when the pool has physical room. Since `limit_size` evicts lowest-fee-rate entries first, this degrades pool throughput and reduces effective pool capacity. The inflation is cumulative across every eviction-triggering `add_entry` call, meaning repeated exploitation progressively shrinks the effective pool. This matches the allowed impact: **High — vulnerabilities or bad designs which could cause CKB network congestion with few costs**, as a sufficiently inflated counter causes the pool to continuously reject or evict valid transactions, degrading mempool capacity network-wide.

## Likelihood Explanation
The eviction branch in `check_and_record_ancestors` fires when `ancestors_count > max_ancestors_count` (default 125) but `ancestors_count - cell_ref_parents.len() <= max_ancestors_count`. An unprivileged attacker must build a chain of ~124 transactions in the pool that also reference a popular cell dep (e.g., secp256k1 lock script), then submit a new transaction referencing the same cell dep. This requires paying fees for ~124 transactions but no special privilege beyond `send_transaction` RPC access. The condition is repeatable: each successful trigger adds more inflation to the counters.

## Recommendation
Move `updated_stat_for_add_tx` (or the final assignment) to **after** `check_and_record_ancestors` completes, so eviction-driven decrements are already reflected in `self.total_tx_size` before the new entry's size is added:

```rust
evicts = self.check_and_record_ancestors(&mut entry)?;
self.record_entry_edges(&entry)?;
self.insert_entry(&entry, status);
self.record_entry_descendants(&entry);
self.track_entry_statics(None, Some(status));
// Compute AFTER evictions have already updated self.total_tx_size
let (total_tx_size, total_tx_cycles) =
    self.updated_stat_for_add_tx(entry.size, entry.cycles)?;
self.total_tx_size = total_tx_size;
self.total_tx_cycles = total_tx_cycles;
```

Alternatively, remove the locals entirely and perform the increment in-place after all mutations complete.

## Proof of Concept
1. Submit ~124 transactions to the pool that all reference the secp256k1 cell dep, forming a chain (tx1 → tx2 → … → tx124).
2. Submit a new transaction tx125 that (a) spends an output of tx124 and (b) also references the secp256k1 cell dep, pushing `ancestors_count` to 125+.
3. `check_and_record_ancestors` enters the eviction branch (line 603), removes some `cell_ref_parents` via `remove_entry_and_descendants`, and correctly decrements `self.total_tx_size`.
4. `add_entry` then overwrites `self.total_tx_size` with the stale pre-eviction value (lines 218-219).
5. Query `tx_pool_info` via RPC: `total_tx_size` will exceed the sum of all actual entries' sizes.
6. Repeat steps 1-4 multiple times; each iteration inflates the counter further until `limit_size` begins evicting legitimate transactions even though the pool has physical room.
7. A unit test can assert the invariant `pool_map.total_tx_size == pool_map.entries.iter().map(|e| e.size).sum()` before and after a triggered eviction to confirm the discrepancy. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4)

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

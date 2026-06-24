The code confirms the claim exactly. Let me verify the `remove_entry_and_descendants` path to complete the analysis.

Audit Report

## Title
`total_tx_size`/`total_tx_cycles` Overwritten with Stale Pre-Eviction Values After Ancestor Eviction in `add_entry` — (File: `tx-pool/src/component/pool_map.rs`)

## Summary

In `PoolMap::add_entry`, the new totals for `total_tx_size` and `total_tx_cycles` are captured into local variables before `check_and_record_ancestors` runs. When that function evicts entries via `remove_entry_and_descendants`, it correctly decrements `self.total_tx_size`/`self.total_tx_cycles` through `update_stat_for_remove_tx`. However, `add_entry` then unconditionally overwrites `self.total_tx_size` and `self.total_tx_cycles` with the stale pre-eviction locals, erasing the eviction's accounting effect. The result is a permanent, cumulative overcount of pool size and cycles, causing `limit_size` to evict legitimate transactions unnecessarily and causing future valid submissions to be rejected with `Reject::Full` even when the pool has real capacity.

## Finding Description

In `add_entry` (lines 200–221 of `tx-pool/src/component/pool_map.rs`):

```
// Step 1: capture pre-eviction totals into locals
let (total_tx_size, total_tx_cycles) =
    self.updated_stat_for_add_tx(entry.size, entry.cycles)?;   // reads self.total_tx_size at this moment

// Step 2: may evict entries, calling update_stat_for_remove_tx
//         which MODIFIES self.total_tx_size / self.total_tx_cycles in-place
evicts = self.check_and_record_ancestors(&mut entry)?;

// Step 3: OVERWRITES self.total_tx_size with the stale pre-eviction local
self.total_tx_size = total_tx_size;
self.total_tx_cycles = total_tx_cycles;
``` [1](#0-0) 

`updated_stat_for_add_tx` (lines 711–729) is a `&self` method that reads `self.total_tx_size` at call time and returns `self.total_tx_size + entry.size` as a plain local value — it does not update `self` at all. [2](#0-1) 

The eviction branch in `check_and_record_ancestors` (lines 603–625) is entered when `ancestors_count > max_ancestors_count` but `ancestors_count - cell_ref_parents.len() <= max_ancestors_count`. It calls `remove_entry_and_descendants`, which calls `remove_entry`, which calls `update_stat_for_remove_tx` — directly mutating `self.total_tx_size` and `self.total_tx_cycles` in-place. [3](#0-2) [4](#0-3) [5](#0-4) 

**Concrete accounting drift:** If before `add_entry` `self.total_tx_size = X`, the new entry has size `S`, and evictions remove entries of total size `E`:
- Correct final value: `X - E + S`
- Actual final value: `X + S` (overcounted by `E`)

The drift is permanent — there is no subsequent recomputation that corrects it. Each eviction event during `add_entry` adds more drift.

## Impact Explanation

`limit_size` (called after `add_entry` in the submission flow) loops while `self.pool_map.total_tx_size > self.config.max_tx_pool_size`, evicting entries until the inflated counter drops below the limit. [6](#0-5) 

With `total_tx_size` overcounted by `E`, `limit_size` will evict `E` bytes worth of additional legitimate transactions that should not have been removed. Simultaneously, future `add_entry` calls will fail with `Reject::Full` via `updated_stat_for_add_tx` even when the pool has real available capacity, because the inflated counter makes the pool appear full. An attacker who repeatedly triggers this path accumulates drift without bound, eventually making the pool reject all new transactions and evict all existing ones — a sustained mempool DoS. This matches the **High** impact class: *Vulnerabilities or bad designs which could cause CKB network congestion with few costs*.

## Likelihood Explanation

The eviction branch in `check_and_record_ancestors` is reachable by any unprivileged RPC caller via `send_transaction`. The attacker submits a sequence of valid transactions forming a chain where some ancestors are referenced as cell-deps, pushing `ancestors_count` above `max_ancestors_count` while `cell_ref_parents` brings it back within limit. This is a normal, supported transaction pattern. No privileged access, leaked keys, or majority hashpower is required. The attacker can repeat the pattern in a loop, accumulating drift with each iteration, at the cost of only transaction fees for the submitted (and subsequently evicted) transactions.

## Recommendation

Move the stat update to **after** `check_and_record_ancestors` returns, so it accounts for any evictions that already decremented `self.total_tx_size`/`self.total_tx_cycles`:

```rust
evicts = self.check_and_record_ancestors(&mut entry)?;
self.record_entry_edges(&entry)?;
self.insert_entry(&entry, status);
self.record_entry_descendants(&entry);
self.track_entry_statics(None, Some(status));
// Recompute AFTER evictions have already updated self.total_tx_size/cycles
let (total_tx_size, total_tx_cycles) =
    self.updated_stat_for_add_tx(entry.size, entry.cycles)?;
self.total_tx_size = total_tx_size;
self.total_tx_cycles = total_tx_cycles;
```

Alternatively, replace the local-variable pattern entirely with a direct in-place increment after all evictions are complete, mirroring the pattern used by `update_stat_for_remove_tx`. [7](#0-6) 

## Proof of Concept

1. Fill the pool with a chain `T1 → T2 → ... → Tn` where `Tn` is referenced as a cell-dep by a side transaction `S1`, making `S1`'s ancestor count exceed `max_ancestors_count`.
2. Submit a new transaction `T_new` whose ancestor set includes `S1` as a cell-dep parent, triggering the eviction branch in `check_and_record_ancestors` (lines 603–625).
3. `remove_entry_and_descendants(S1)` is called, decrementing `self.total_tx_size` by `size(S1)` via `update_stat_for_remove_tx`.
4. `add_entry` then sets `self.total_tx_size = old_total + size(T_new)`, ignoring the `size(S1)` decrement.
5. Query `tx_pool_info` RPC and observe `total_tx_size` is `size(S1)` bytes larger than the actual sum of entries in the pool.
6. Repeat steps 1–4 to accumulate drift. Observe that `limit_size` begins evicting legitimate transactions, and further valid submissions are rejected with `Reject::Full` despite the pool having real capacity. [8](#0-7)

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

**File:** tx-pool/src/component/pool_map.rs (L235-250)
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
    }
```

**File:** tx-pool/src/component/pool_map.rs (L588-640)
```rust
    fn check_and_record_ancestors(
        &mut self,
        entry: &mut TxEntry,
    ) -> Result<HashSet<TxEntry>, Reject> {
        let tx = entry.transaction();
        let (ancestors, mut parents, cell_ref_parents) = self.get_tx_ancenstors(tx);

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

        // some txs in `parents` are removed, now `ancestors` need to re-caculate,
        let ancestors = self
            .links
            .calc_relation_ids(parents.clone(), Relation::Parents);

        // we can assume the number now is less than `max_ancestors_count`
        assert!(ancestors.len() < self.max_ancestors_count);

        self._record_ancestors(entry, ancestors, parents);
        Ok(evicted)
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

**File:** tx-pool/src/pool.rs (L298-327)
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
        self.pool_map.entries.shrink_to_fit();
```

Audit Report

## Title
`total_tx_size`/`total_tx_cycles` Stale Snapshot Overwrite in `add_entry()` After Ancestor-Eviction — (`tx-pool/src/component/pool_map.rs`)

## Summary

In `PoolMap::add_entry()`, `updated_stat_for_add_tx()` snapshots `self.total_tx_size + entry.size` into a local variable before `check_and_record_ancestors()` runs. When `check_and_record_ancestors()` evicts `cell_ref_parent` transactions via `remove_entry_and_descendants()`, each removal correctly decrements `self.total_tx_size` through `update_stat_for_remove_tx()`. However, `add_entry()` then unconditionally overwrites `self.total_tx_size` with the pre-eviction snapshot, erasing those decrements. The result is that `total_tx_size` is inflated by the aggregate size of all entries evicted during ancestor resolution, causing `limit_size()` to expel legitimate transactions from a pool that has real remaining capacity.

## Finding Description

`add_entry()` at lines 210–219 of `tx-pool/src/component/pool_map.rs`:

```rust
// snapshot taken BEFORE any evictions
let (total_tx_size, total_tx_cycles) =
    self.updated_stat_for_add_tx(entry.size, entry.cycles)?;  // L210-211

// may call remove_entry_and_descendants → remove_entry → update_stat_for_remove_tx
// which correctly decrements self.total_tx_size for each evicted entry
evicts = self.check_and_record_ancestors(&mut entry)?;         // L213

// ...insert new entry...

// OVERWRITES the correctly-decremented self.total_tx_size with the stale snapshot
self.total_tx_size = total_tx_size;    // L218
self.total_tx_cycles = total_tx_cycles; // L219
```

`updated_stat_for_add_tx()` (L711–729) computes `self.total_tx_size.checked_add(tx_size)` at call time and returns the result as a plain `usize` — it does not modify `self`. [1](#0-0) 

`check_and_record_ancestors()` (L588–640) enters the eviction branch when `ancestors_count > max_ancestors_count` and `cell_ref_parents` is non-empty. It calls `self.remove_entry_and_descendants(next_id)` (L618), which chains to `remove_entry()` (L235–250), which calls `self.update_stat_for_remove_tx(entry.inner.size, entry.inner.cycles)` (L247). [2](#0-1) 

`update_stat_for_remove_tx()` (L733–758) correctly decrements `self.total_tx_size` and `self.total_tx_cycles` in place. [3](#0-2) 

The overwrite at L218–219 then restores the pre-eviction value, inflating `self.total_tx_size` by exactly `Σ size(evicted_entry)` for all entries removed during ancestor resolution. [4](#0-3) 

`limit_size()` in `tx-pool/src/pool.rs` (L298) loops `while self.pool_map.total_tx_size > self.config.max_tx_pool_size`, evicting lowest-fee-rate entries until the condition is false. With an inflated counter, it evicts legitimate transactions even though actual pool occupancy is below the limit. [5](#0-4) 

## Impact Explanation

Each successful trigger inflates `total_tx_size` by the size of the evicted `cell_ref_parent` transaction(s). `limit_size()` then expels legitimate pending transactions to compensate for the phantom inflation. Repeated triggering progressively shrinks the effective pool capacity, causing valid user transactions to be rejected or expelled and delaying or preventing their on-chain confirmation. This matches the allowed impact: **"Vulnerabilities or bad designs which could cause CKB network congestion with few costs" — High (10001–15000 points)**. The attacker pays only normal transaction fees for a chain of ~26 transactions per trigger cycle; no hashpower or privileged access is required.

## Likelihood Explanation

The eviction branch in `check_and_record_ancestors()` is reachable by any unprivileged `send_transaction` RPC caller. The required conditions are: (1) a new transaction has more than `max_ancestors_count` (default 25) in-pool ancestors, and (2) at least one ancestor is a `cell_ref_parent` whose removal brings the count within the limit. Both conditions are fully attacker-controlled by constructing a chain A→C1→…→C24, a side transaction B using A's output as a cell dep, and a final transaction D spending C24's output and A's output as inputs. D has 26 ancestors; B is a `cell_ref_parent` and gets evicted, triggering the overwrite. The attack is repeatable with fresh transaction chains at the cost of standard fees. [6](#0-5) 

## Recommendation

Perform the overflow check early (to reject oversized submissions promptly) but do **not** write the result back to `self` until after all evictions are complete. Replace the pre-computation pattern in `add_entry()` with:

```rust
// Early overflow guard only — do not assign to self yet
self.updated_stat_for_add_tx(entry.size, entry.cycles)?;

evicts = self.check_and_record_ancestors(&mut entry)?;
self.record_entry_edges(&entry)?;
self.insert_entry(&entry, status);
self.record_entry_descendants(&entry);
self.track_entry_statics(None, Some(status));

// Increment AFTER all evictions; self.total_tx_size already reflects removals
self.total_tx_size = self.total_tx_size
    .checked_add(entry.size)
    .expect("overflow already checked above");
self.total_tx_cycles = self.total_tx_cycles
    .checked_add(entry.cycles)
    .expect("overflow already checked above");
```

This preserves the early rejection semantics while ensuring the final counter reflects the true pool state after evictions.

## Proof of Concept

**Preconditions:** `max_tx_pool_size = 1000`, `max_ancestors_count = 25`, pool holds 900 bytes, `total_tx_size = 900`.

1. Submit tx A (creates outputs X and Y).
2. Submit tx B (size 100, uses output X as a cell dep). `total_tx_size = 1000`.
3. Submit chain A→C1→…→C24 (each spending the previous output). `total_tx_size` grows accordingly.
4. Submit tx D (size 50, spends C24's output and output X as inputs).
   - D has 26 ancestors (A, C1–C24, B) > 25.
   - B is a `cell_ref_parent`; `check_and_record_ancestors()` evicts B, decrementing `self.total_tx_size` by 100.
   - `add_entry()` then overwrites `self.total_tx_size` with the pre-eviction snapshot (which included B's 100 bytes), inflating it by 100.
5. `limit_size()` now sees `total_tx_size` exceeding `max_tx_pool_size` and evicts a legitimate transaction, even though actual pool occupancy is within the limit.
6. Repeat steps 1–5 with fresh transactions to progressively shrink effective pool capacity.

A unit test can assert `pool_map.total_tx_size == pool_map.entries.iter().map(|e| e.size).sum::<usize>()` immediately after step 4 to confirm the invariant is broken. [7](#0-6)

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

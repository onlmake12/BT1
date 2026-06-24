The code confirms the claim exactly. All cited lines match the actual source:

- [1](#0-0)  — `updated_stat_for_add_tx` is called at line 211 (before evictions), capturing stale locals, which are then unconditionally written back at lines 218–219 after `check_and_record_ancestors` (line 213) may have already mutated `self.total_tx_size`/`self.total_tx_cycles` via evictions.
- [2](#0-1)  — `updated_stat_for_add_tx` is a `&self` method returning plain locals; it does not update `self`.
- [3](#0-2)  — The eviction branch calls `remove_entry_and_descendants`, which calls `remove_entry` → `update_stat_for_remove_tx`, directly mutating `self.total_tx_size`/`self.total_tx_cycles` in-place.
- [4](#0-3)  — `update_stat_for_remove_tx` directly assigns to `self.total_tx_size`/`self.total_tx_cycles`.
- [5](#0-4)  — `limit_size` loops on the inflated counter, causing over-eviction.

The bug is real and all code references are accurate.

---

Audit Report

## Title
`total_tx_size`/`total_tx_cycles` Overwritten with Stale Pre-Eviction Values After Ancestor Eviction in `add_entry` — (File: `tx-pool/src/component/pool_map.rs`)

## Summary

In `PoolMap::add_entry`, pre-eviction totals are captured into locals before `check_and_record_ancestors` runs. When that function evicts entries via `remove_entry_and_descendants`, it correctly decrements `self.total_tx_size`/`self.total_tx_cycles` through `update_stat_for_remove_tx`. However, `add_entry` then unconditionally overwrites `self.total_tx_size` and `self.total_tx_cycles` with the stale pre-eviction locals, erasing the eviction's accounting effect. The result is a permanent, cumulative overcount of pool size and cycles, enabling a sustained mempool DoS.

## Finding Description

In `add_entry` (lines 200–221 of `tx-pool/src/component/pool_map.rs`):

```rust
// Step 1: capture pre-eviction totals into locals
let (total_tx_size, total_tx_cycles) =
    self.updated_stat_for_add_tx(entry.size, entry.cycles)?;   // reads self.total_tx_size at this moment

// Step 2: may evict entries, calling update_stat_for_remove_tx
//         which MODIFIES self.total_tx_size / self.total_tx_cycles in-place
evicts = self.check_and_record_ancestors(&mut entry)?;

// Step 3: OVERWRITES self.total_tx_size with the stale pre-eviction local
self.total_tx_size = total_tx_size;
self.total_tx_cycles = total_tx_cycles;
```

`updated_stat_for_add_tx` (lines 711–729) is a `&self` method that reads `self.total_tx_size` at call time and returns `self.total_tx_size + entry.size` as a plain local value — it does not update `self` at all.

The eviction branch in `check_and_record_ancestors` (lines 603–625) is entered when `ancestors_count > max_ancestors_count` but `ancestors_count - cell_ref_parents.len() <= max_ancestors_count`. It calls `remove_entry_and_descendants`, which calls `remove_entry`, which calls `update_stat_for_remove_tx` — directly mutating `self.total_tx_size` and `self.total_tx_cycles` in-place.

**Concrete accounting drift:** If before `add_entry` `self.total_tx_size = X`, the new entry has size `S`, and evictions remove entries of total size `E`:
- Correct final value: `X - E + S`
- Actual final value: `X + S` (overcounted by `E`)

The drift is permanent — there is no subsequent recomputation that corrects it. Each eviction event during `add_entry` adds more drift.

## Impact Explanation

`limit_size` (called after `add_entry` in the submission flow) loops while `self.pool_map.total_tx_size > self.config.max_tx_pool_size`, evicting entries until the inflated counter drops below the limit. With `total_tx_size` overcounted by `E`, `limit_size` will evict `E` bytes worth of additional legitimate transactions that should not have been removed. Simultaneously, future `add_entry` calls will fail with `Reject::Full` via `updated_stat_for_add_tx` even when the pool has real available capacity, because the inflated counter makes the pool appear full. An attacker who repeatedly triggers this path accumulates drift without bound, eventually making the pool reject all new transactions and evict all existing ones — a sustained mempool DoS. This matches the **High** impact class: *Vulnerabilities or bad designs which could cause CKB network congestion with few costs*.

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

Alternatively, replace the local-variable pattern entirely with a direct in-place increment after all evictions are complete, mirroring the pattern used by `update_stat_for_remove_tx`.

## Proof of Concept

1. Fill the pool with a chain `T1 → T2 → ... → Tn` where `Tn` is referenced as a cell-dep by a side transaction `S1`, making `S1`'s ancestor count exceed `max_ancestors_count`.
2. Submit a new transaction `T_new` whose ancestor set includes `S1` as a cell-dep parent, triggering the eviction branch in `check_and_record_ancestors` (lines 603–625).
3. `remove_entry_and_descendants(S1)` is called, decrementing `self.total_tx_size` by `size(S1)` via `update_stat_for_remove_tx`.
4. `add_entry` then sets `self.total_tx_size = old_total + size(T_new)`, ignoring the `size(S1)` decrement.
5. Query `tx_pool_info` RPC and observe `total_tx_size` is `size(S1)` bytes larger than the actual sum of entries in the pool.
6. Repeat steps 1–4 to accumulate drift. Observe that `limit_size` begins evicting legitimate transactions, and further valid submissions are rejected with `Reject::Full` despite the pool having real capacity.

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

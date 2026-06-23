### Title
tx-pool `total_tx_size`/`total_tx_cycles` Accounting Corrupted by Eviction During `add_entry` - (File: tx-pool/src/component/pool_map.rs)

---

### Summary

In `PoolMap::add_entry`, the pool's aggregate size and cycle counters (`total_tx_size`, `total_tx_cycles`) are computed **before** a potential in-place eviction of existing entries, then unconditionally written back **after** the eviction. The eviction's own decrements to those counters are silently overwritten, permanently inflating the pool's reported resource usage. An unprivileged `send_transaction` caller can exploit this to make the pool appear full, causing legitimate transactions to be rejected with `PoolIsFull`.

---

### Finding Description

`PoolMap::add_entry` follows this sequence:

```
1. (total_tx_size, total_tx_cycles) = self.total_tx_size + entry.size   // snapshot BEFORE eviction
2. evicts = self.check_and_record_ancestors(&mut entry)                  // may evict entries
      └─ remove_entry_and_descendants(next_id)
            └─ remove_entry(id)
                  └─ update_stat_for_remove_tx(size, cycles)             // decrements self.total_tx_size
3. self.total_tx_size  = total_tx_size   // OVERWRITES with pre-eviction snapshot
4. self.total_tx_cycles = total_tx_cycles
```

The snapshot in step 1 is taken from `self.total_tx_size` before any evictions. Step 2 (`check_and_record_ancestors`) can evict one or more existing pool entries via `remove_entry_and_descendants`, each of which calls `update_stat_for_remove_tx` and correctly decrements `self.total_tx_size`. However, steps 3–4 then unconditionally overwrite `self.total_tx_size` with the pre-eviction snapshot value, discarding all decrements performed during step 2.

The eviction path inside `check_and_record_ancestors` is triggered when:
- The incoming transaction has more than `max_ancestors_count` ancestors (default 25), **and**
- Some of those ancestors are `cell_ref_parents` — pool transactions that reference (as a `cell_dep`) an output that the new transaction's input is consuming.

When this condition holds, the code evicts those `cell_ref_parents` to bring the ancestor count within the limit. Each evicted entry's size is subtracted from `self.total_tx_size` inside `remove_entry`, but the final assignment in `add_entry` erases those subtractions.

**Root cause lines:** [1](#0-0) 

The pre-eviction snapshot is captured at lines 210–211, evictions happen at line 213 (via `check_and_record_ancestors`), and the stale snapshot is written back at lines 218–219, overwriting the decrements applied by: [2](#0-1) 

which is called from: [3](#0-2) 

The eviction loop itself: [4](#0-3) 

---

### Impact Explanation

`total_tx_size` is the authoritative counter used by:

1. **`limit_size`** — evicts entries while `total_tx_size > max_tx_pool_size`. An inflated counter causes legitimate, already-accepted transactions to be evicted unnecessarily.
2. **`updated_stat_for_add_tx`** — rejects new submissions with `Reject::Full` when the counter exceeds the pool limit. An inflated counter causes valid transactions to be rejected with `PoolIsFull` even when actual pool space is available.
3. **`tx_pool_info` RPC** — reports incorrect pool occupancy to operators and tooling. [5](#0-4) [6](#0-5) 

Each successful exploit inflates `total_tx_size` by the serialized size of the evicted transaction(s). The inflation is permanent until the pool is cleared. Repeated exploitation accumulates inflation, eventually making the pool appear full when it is nearly empty, constituting a sustained denial-of-service against transaction submission.

---

### Likelihood Explanation

The trigger condition — a new transaction whose ancestor count exceeds `max_ancestors_count` but can be reduced to within the limit by evicting `cell_ref_parents` — is fully constructible by any unprivileged `send_transaction` caller:

1. Submit a base transaction `tx_base` with two outputs: `out_Y` and `out_X`.
2. Submit a chain `tx1 → tx2 → … → tx25` where `tx1` spends `out_Y` (giving 25 ancestors).
3. Submit `txA` that uses `out_X` as a `cell_dep` (making `txA` a `cell_ref_parent` for any future tx that spends `out_X`).
4. Submit `txNew` that spends both `tx25`'s output **and** `out_X`. This gives `txNew` 26 ancestors (tx_base + tx1..tx25), exceeding `max_ancestors_count = 25`. Since `txA` is a `cell_ref_parent` and `26 - 1 = 25 ≤ 25`, the eviction path fires, evicts `txA`, and then `total_tx_size` is inflated by `txA`'s size.

No privileged access, no majority hashpower, and no social engineering is required. The attack is repeatable with fresh transaction chains. [7](#0-6) 

---

### Recommendation

Compute the pre-eviction snapshot **after** `check_and_record_ancestors` returns (i.e., after all evictions have been applied to `self.total_tx_size`), or alternatively, do not use a snapshot at all and instead apply the new entry's contribution incrementally after evictions complete:

```rust
// After check_and_record_ancestors (evictions done), then add the new entry's contribution:
evicts = self.check_and_record_ancestors(&mut entry)?;
self.record_entry_edges(&entry)?;
self.insert_entry(&entry, status);
self.record_entry_descendants(&entry);
self.track_entry_statics(None, Some(status));
// Apply new entry's size/cycles AFTER evictions have already decremented the counters:
self.total_tx_size = self.total_tx_size.checked_add(entry.size)
    .ok_or_else(|| Reject::Full(...))?;
self.total_tx_cycles = self.total_tx_cycles.checked_add(entry.cycles)
    .ok_or_else(|| Reject::Full(...))?;
```

The overflow guard (currently in `updated_stat_for_add_tx`) should still be applied, but against the post-eviction counter value.

---

### Proof of Concept

```
State: max_ancestors_count = 25, pool empty, total_tx_size = 0

Step 1: Submit tx_base (size=200) → total_tx_size = 200
Step 2: Submit tx1..tx25 chained from tx_base (each size=200) → total_tx_size = 5200
Step 3: Submit txA (size=500) using tx_base.out_X as cell_dep → total_tx_size = 5700

Step 4: Submit txNew spending tx25.output AND tx_base.out_X
  - ancestors = {tx_base, tx1..tx25} → ancestors_count = 26 > 25
  - cell_ref_parents = {txA} (txA uses tx_base.out_X as cell_dep, txNew spends it)
  - 26 - 1 = 25 ≤ 25 → eviction path taken
  - snapshot: total_tx_size_snap = 5700 + txNew.size (e.g. 200) = 5900
  - remove_entry_and_descendants(txA): self.total_tx_size = 5700 - 500 = 5200
  - txNew inserted
  - self.total_tx_size = 5900  ← OVERWRITES, losing the -500 decrement

Expected total_tx_size after step 4: 5200 (tx_base + tx1..tx25 + txNew) = 5400
Actual total_tx_size after step 4:   5900  ← inflated by 500 (txA's size)

Repeating this pattern N times inflates total_tx_size by N×500,
eventually causing all new send_transaction calls to return PoolIsFull.
```

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

**File:** tx-pool/src/component/pool_map.rs (L247-247)
```rust
            self.update_stat_for_remove_tx(entry.inner.size, entry.inner.cycles);
```

**File:** tx-pool/src/component/pool_map.rs (L598-628)
```rust
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

**File:** tx-pool/src/component/pool_map.rs (L710-728)
```rust
    /// Calculate size and cycles statistics for adding a tx.
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

**File:** tx-pool/src/component/pool_map.rs (L733-757)
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
```

**File:** tx-pool/src/pool.rs (L292-328)
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
```

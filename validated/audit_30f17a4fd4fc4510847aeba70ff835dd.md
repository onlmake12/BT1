### Title
`total_tx_size` / `total_tx_cycles` Accounting Desync When Ancestor-Eviction Occurs Inside `add_entry` — (File: `tx-pool/src/component/pool_map.rs`)

---

### Summary

`PoolMap::add_entry` pre-computes updated `total_tx_size` / `total_tx_cycles` totals **before** calling `check_and_record_ancestors`, which may itself evict pool entries (via `remove_entry_and_descendants` → `update_stat_for_remove_tx`). The pre-computed totals are then unconditionally written back, silently overwriting the correctly-decremented values and permanently inflating both counters.

---

### Finding Description

`PoolMap` maintains two cumulative accounting fields:

```rust
pub(crate) total_tx_size: usize,
pub(crate) total_tx_cycles: Cycle,
``` [1](#0-0) 

`add_entry` updates them in three distinct steps:

**Step 1** — pre-compute new totals (reads current `self.total_tx_size`):
```rust
let (total_tx_size, total_tx_cycles) =
    self.updated_stat_for_add_tx(entry.size, entry.cycles)?;
``` [2](#0-1) 

**Step 2** — potentially evict existing entries (modifies `self.total_tx_size` via `update_stat_for_remove_tx`):
```rust
evicts = self.check_and_record_ancestors(&mut entry)?;
``` [3](#0-2) 

Inside `check_and_record_ancestors`, when `ancestors_count > max_ancestors_count` but the excess is attributable to cell-dep parents, entries are evicted:
```rust
let removed = self.remove_entry_and_descendants(next_id);
``` [4](#0-3) 

Each `remove_entry` call correctly decrements `self.total_tx_size` and `self.total_tx_cycles`:
```rust
self.update_stat_for_remove_tx(entry.inner.size, entry.inner.cycles);
``` [5](#0-4) 

**Step 3** — unconditionally overwrite with the stale pre-computed value:
```rust
self.total_tx_size = total_tx_size;
self.total_tx_cycles = total_tx_cycles;
``` [6](#0-5) 

The decrements applied in Step 2 are completely discarded. The final stored value equals `old_total + entry.size` rather than the correct `old_total − Σ(evicted_sizes) + entry.size`.

**Concrete example:**
- `self.total_tx_size = 1000`, new entry size = 100, evicted entry size = 200
- Step 1 computes local `total_tx_size = 1100`
- Step 2 sets `self.total_tx_size = 800` (correct decrement)
- Step 3 writes `self.total_tx_size = 1100` (stale value)
- Correct answer: `900`; actual stored: `1100` — inflated by 200

---

### Impact Explanation

`total_tx_size` is the sole guard used by `limit_size` to enforce the pool capacity ceiling:

```rust
while self.pool_map.total_tx_size > self.config.max_tx_pool_size {
    // evict lowest-fee-rate entries
}
``` [7](#0-6) 

An inflated `total_tx_size` causes `limit_size` to over-evict legitimate pending/proposed transactions that would otherwise remain in the pool. Each subsequent insertion that triggers the eviction path compounds the inflation. The corrupted values are also surfaced directly to RPC callers via `get_tx_pool_info`:

```rust
total_tx_size: tx_pool.pool_map.total_tx_size,
total_tx_cycles: tx_pool.pool_map.total_tx_cycles,
``` [8](#0-7) 

The `recompute_total_stat` fallback in `update_stat_for_remove_tx` only triggers on underflow, not on inflation, so it provides no protection here. [9](#0-8) 

---

### Likelihood Explanation

The eviction path inside `check_and_record_ancestors` is triggered when a submitted transaction has `ancestors_count > max_ancestors_count` but the excess is entirely attributable to cell-dep-referencing parents (`cell_ref_parents`). A transaction sender can deliberately construct a transaction chain where some ancestors are referenced via cell deps rather than inputs, satisfying this condition. The default `max_ancestors_count` is 25, a reachable depth for a crafted chain. No privileged access is required — any unprivileged tx-pool submitter can trigger this path via the standard `send_transaction` RPC.

---

### Recommendation

Move the `updated_stat_for_add_tx` call to **after** `check_and_record_ancestors` completes, so the pre-computation reads the already-eviction-adjusted `self.total_tx_size`:

```rust
evicts = self.check_and_record_ancestors(&mut entry)?;
self.record_entry_edges(&entry)?;
let (total_tx_size, total_tx_cycles) =
    self.updated_stat_for_add_tx(entry.size, entry.cycles)?;
self.insert_entry(&entry, status);
self.record_entry_descendants(&entry);
self.track_entry_statics(None, Some(status));
self.total_tx_size = total_tx_size;
self.total_tx_cycles = total_tx_cycles;
```

Alternatively, apply the increment in-place after evictions rather than pre-computing and overwriting.

---

### Proof of Concept

1. Fill the pool with a chain of 24 transactions `T1 → T2 → … → T24` where each `Ti` is an input-parent of `Ti+1`.
2. Add transaction `A` that takes a cell dep on `T1` (making `T1` a `cell_ref_parent` of `A`).
3. Submit transaction `B` whose inputs spend `T24`'s output AND whose cell deps reference `A`'s output. `B` now has 25 input-ancestors + 1 cell-dep ancestor = 26 total, exceeding `max_ancestors_count = 25`. Since `cell_ref_parents = {A}`, `26 - 1 = 25 ≤ 25`, so the eviction branch fires and evicts `A` (and its descendants).
4. After `B` is inserted, query `get_tx_pool_info`. `total_tx_size` will be inflated by `A`'s serialized size.
5. Repeat with additional crafted transactions to compound the inflation until `limit_size` begins evicting legitimate transactions that are well within the actual pool byte budget. [10](#0-9) [11](#0-10)

### Citations

**File:** tx-pool/src/component/pool_map.rs (L68-75)
```rust
    // sum of all tx_pool tx's virtual sizes.
    pub(crate) total_tx_size: usize,
    // sum of all tx_pool tx's cycles.
    pub(crate) total_tx_cycles: Cycle,
    pub(crate) pending_count: usize,
    pub(crate) gap_count: usize,
    pub(crate) proposed_count: usize,
}
```

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

**File:** tx-pool/src/component/pool_map.rs (L247-247)
```rust
            self.update_stat_for_remove_tx(entry.inner.size, entry.inner.cycles);
```

**File:** tx-pool/src/component/pool_map.rs (L598-639)
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

        // some txs in `parents` are removed, now `ancestors` need to re-caculate,
        let ancestors = self
            .links
            .calc_relation_ids(parents.clone(), Relation::Parents);

        // we can assume the number now is less than `max_ancestors_count`
        assert!(ancestors.len() < self.max_ancestors_count);

        self._record_ancestors(entry, ancestors, parents);
        Ok(evicted)
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

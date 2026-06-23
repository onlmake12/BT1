### Title
Stale Pre-Computed `total_tx_size`/`total_tx_cycles` Overwrites Correct Post-Eviction Values in `PoolMap::add_entry` — (File: `tx-pool/src/component/pool_map.rs`)

---

### Summary

In `PoolMap::add_entry`, two critical accounting variables — `total_tx_size` and `total_tx_cycles` — are computed before a conditional eviction step, then unconditionally written back after the eviction. When the eviction path inside `check_and_record_ancestors` fires, it correctly decrements those variables via `update_stat_for_remove_tx`, but the final assignment overwrites those decrements with the stale pre-eviction values. The result is that `total_tx_size` and `total_tx_cycles` become permanently inflated by the size and cycles of every evicted entry, causing the pool to believe it is fuller than it actually is.

---

### Finding Description

`PoolMap` maintains two aggregate counters that gate pool admission and eviction:

```
total_tx_size: usize   // line 69
total_tx_cycles: Cycle // line 71
``` [1](#0-0) 

`add_entry` follows this sequence:

1. **Pre-compute** new totals (before any eviction):
   ```rust
   let (total_tx_size, total_tx_cycles) =
       self.updated_stat_for_add_tx(entry.size, entry.cycles)?;
   ``` [2](#0-1) 

2. **Conditionally evict** entries via `check_and_record_ancestors`, which calls `remove_entry_and_descendants` → `remove_entry` → `update_stat_for_remove_tx`, **mutating** `self.total_tx_size` and `self.total_tx_cycles` in place: [3](#0-2) [4](#0-3) 

3. **Unconditionally overwrite** with the stale pre-eviction values:
   ```rust
   self.total_tx_size = total_tx_size;
   self.total_tx_cycles = total_tx_cycles;
   ``` [5](#0-4) 

The eviction path in `check_and_record_ancestors` fires when the incoming transaction's ancestor count exceeds `max_ancestors_count`, but the excess is attributable to `cell_ref_parents` — pool transactions that reference the new transaction's input out-points as cell deps: [6](#0-5) 

`update_stat_for_remove_tx` correctly decrements both fields for each evicted entry: [7](#0-6) 

But step 3 above discards those decrements. Concretely, if the pool starts at `total_tx_size = T` and an entry of size `S_evict` is evicted while a new entry of size `S_new` is added:

- **Correct final value:** `T − S_evict + S_new`
- **Actual final value:** `T + S_new` (inflated by `S_evict`)

Each trigger of this path permanently inflates both counters by the size/cycles of the evicted entries.

---

### Impact Explanation

`total_tx_size` is the sole guard for pool-size enforcement:

```rust
while self.pool_map.total_tx_size > self.config.max_tx_pool_size {
``` [8](#0-7) 

And it is the basis for the overflow check that rejects new transactions: [9](#0-8) 

When `total_tx_size` is inflated:

1. **Spurious eviction:** `limit_size` evicts legitimate pending/proposed transactions even though the pool has real capacity, degrading miner revenue and user experience.
2. **Spurious rejection:** `updated_stat_for_add_tx` returns `Reject::Full` for new transactions that would fit, blocking honest users from submitting transactions.
3. **Incorrect RPC reporting:** `get_tx_pool_info` returns a `total_tx_size` larger than reality, misleading operators and fee-estimation clients.

The same inflation applies to `total_tx_cycles`, which gates cycle-based admission checks. [10](#0-9) 

---

### Likelihood Explanation

The eviction path requires:

1. A chain of ≥ `max_ancestors_count` (default 25) transactions already in the pool.
2. At least one of those transactions uses an output that the new transaction spends as an input, as a cell dep (`cell_ref_parents` non-empty).
3. Removing the `cell_ref_parents` brings the ancestor count back within the limit.

An unprivileged transaction sender can deliberately construct this scenario: submit a 25-deep chain where one transaction uses a specific UTXO as a cell dep, then submit a transaction spending that UTXO. Each such submission inflates the counters by the evicted entry's size. Repeating this drives `total_tx_size` arbitrarily above the true pool size, eventually causing the pool to continuously evict or reject legitimate transactions.

---

### Recommendation

Compute the new totals **after** all evictions complete, using the current (post-eviction) values of `self.total_tx_size` and `self.total_tx_cycles`:

```rust
// After check_and_record_ancestors, record_entry_edges, insert_entry, etc.
self.total_tx_size = self.total_tx_size
    .checked_add(entry.size)
    .ok_or_else(|| Reject::Full(...))?;
self.total_tx_cycles = self.total_tx_cycles
    .checked_add(entry.cycles)
    .ok_or_else(|| Reject::Full(...))?;
```

Alternatively, move `updated_stat_for_add_tx` to after `check_and_record_ancestors` returns, so it reads the already-decremented `self.total_tx_size`/`total_tx_cycles`. This mirrors the fix pattern described in the reference report: encapsulate each state update in a dedicated function called at the correct point in the operation sequence, ensuring add and remove operations are always complementary and consistent.

---

### Proof of Concept

**Setup:**

1. Pool is empty; `max_tx_pool_size = 10_000`, `max_ancestors_count = 25`.
2. Submit a chain of 25 transactions `T1 → T2 → … → T25`, where `T10` uses output `O` (an on-chain UTXO) as a cell dep. Each transaction is ~100 bytes. `total_tx_size = 2500`.
3. Submit transaction `N` that spends output `O` as an input and has `T1…T25` as ancestors (ancestor count = 26 > 25). `T10` is a `cell_ref_parent`. Condition `26 − 1 = 25 ≤ 25` is satisfied.

**Execution of `add_entry` for `N` (size = 100, cycles = 1000):**

- `updated_stat_for_add_tx(100, 1000)` → local `total_tx_size = 2600`, `total_tx_cycles = X+1000`.
- `check_and_record_ancestors` evicts `T10` (size = 100) via `remove_entry_and_descendants` → `update_stat_for_remove_tx(100, ...)` → `self.total_tx_size = 2400`.
- `self.total_tx_size = 2600` (stale value written back). **Inflation: +100.**

**After step 3:** Pool actually contains 25 entries totalling 2500 bytes, but `total_tx_size = 2600`.

**Repeat:** Each iteration inflates `total_tx_size` by 100. After 75 iterations, `total_tx_size` exceeds `max_tx_pool_size = 10_000` while the pool holds only ~2500 bytes of real transactions. `limit_size` begins evicting legitimate transactions, and `updated_stat_for_add_tx` starts returning `Reject::Full` for new honest submissions. [11](#0-10) [12](#0-11) [13](#0-12)

### Citations

**File:** tx-pool/src/component/pool_map.rs (L69-71)
```rust
    pub(crate) total_tx_size: usize,
    // sum of all tx_pool tx's cycles.
    pub(crate) total_tx_cycles: Cycle,
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

**File:** tx-pool/src/component/pool_map.rs (L246-247)
```rust
            self.track_entry_statics(Some(entry.status), None);
            self.update_stat_for_remove_tx(entry.inner.size, entry.inner.cycles);
```

**File:** tx-pool/src/component/pool_map.rs (L515-554)
```rust
    // return (ancestors, parents, cell_ref_parents)
    // `cell_ref_parents` may be invalidate when the tx consuming the cell is submitted
    fn get_tx_ancenstors(
        &self,
        entry: &TransactionView,
    ) -> (
        HashSet<ProposalShortId>,
        HashSet<ProposalShortId>,
        HashSet<ProposalShortId>,
    ) {
        let mut parents: HashSet<ProposalShortId> =
            HashSet::with_capacity(entry.inputs().len() + entry.cell_deps().len());
        let mut cell_ref_parents: HashSet<ProposalShortId> = Default::default();

        for input in entry.inputs() {
            let input_pt = input.previous_output();
            if let Some(deps) = self.edges.deps.get(&input_pt) {
                cell_ref_parents.extend(deps.iter().cloned());
                parents.extend(deps.iter().cloned());
            }

            let id = ProposalShortId::from_tx_hash(&input_pt.tx_hash());
            if self.links.inner.contains_key(&id) {
                parents.insert(id);
            }
        }
        for cell_dep in entry.cell_deps() {
            let dep_pt = cell_dep.out_point();
            let id = ProposalShortId::from_tx_hash(&dep_pt.tx_hash());
            if self.links.inner.contains_key(&id) {
                parents.insert(id);
            }
        }

        let ancestors = self
            .links
            .calc_relation_ids(parents.clone(), Relation::Parents);

        (ancestors, parents, cell_ref_parents)
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

**File:** tx-pool/src/component/pool_map.rs (L716-721)
```rust
        let total_tx_size = self.total_tx_size.checked_add(tx_size).ok_or_else(|| {
            Reject::Full(format!(
                "tx-pool total_tx_size {} overflows by add {}",
                self.total_tx_size, tx_size
            ))
        })?;
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

**File:** tx-pool/src/pool.rs (L298-298)
```rust
        while self.pool_map.total_tx_size > self.config.max_tx_pool_size {
```

**File:** tx-pool/src/service.rs (L1089-1090)
```rust
            total_tx_size: tx_pool.pool_map.total_tx_size,
            total_tx_cycles: tx_pool.pool_map.total_tx_cycles,
```

### Title
`PoolMap::add_entry` Overwrites `total_tx_size`/`total_tx_cycles` After Eviction, Inflating Pool Size Accounting — (`tx-pool/src/component/pool_map.rs`)

---

### Summary

In `PoolMap::add_entry`, the pool's `total_tx_size` and `total_tx_cycles` counters are computed **before** ancestor-eviction occurs, then unconditionally written back **after** eviction. Any entries removed during `check_and_record_ancestors` correctly decrement the counters via `update_stat_for_remove_tx`, but those decrements are immediately overwritten by the stale pre-eviction snapshot. The result is that `total_tx_size` and `total_tx_cycles` are permanently inflated by the size/cycles of every evicted transaction, causing the pool to reject or evict legitimate transactions that should be accepted.

---

### Finding Description

`PoolMap::add_entry` follows this sequence:

```
1. (total_tx_size, total_tx_cycles) = updated_stat_for_add_tx(entry.size, entry.cycles)
   // snapshot = self.total_tx_size + new_size, self.total_tx_cycles + new_cycles
   // self.total_tx_size / self.total_tx_cycles are NOT yet modified

2. evicts = check_and_record_ancestors(&mut entry)
   // may call remove_entry_and_descendants → remove_entry → update_stat_for_remove_tx
   // update_stat_for_remove_tx DOES modify self.total_tx_size and self.total_tx_cycles

3. record_entry_edges(&entry)?
4. insert_entry / record_entry_descendants / track_entry_statics

5. self.total_tx_size  = total_tx_size   // OVERWRITES the post-eviction value
6. self.total_tx_cycles = total_tx_cycles // OVERWRITES the post-eviction value
```

`updated_stat_for_add_tx` is a **pure read** — it captures `self.total_tx_size` and `self.total_tx_cycles` at call time and returns new values without modifying the fields. [1](#0-0) 

`check_and_record_ancestors`, when the ancestor count exceeds `max_ancestors_count` but can be reduced by evicting cell-dep parents, calls `remove_entry_and_descendants`, which calls `remove_entry`, which calls `update_stat_for_remove_tx` — **directly mutating** `self.total_tx_size` and `self.total_tx_cycles`. [2](#0-1) [3](#0-2) [4](#0-3) 

After eviction, the stale snapshot is written back unconditionally: [5](#0-4) 

**Concrete arithmetic:**

| Step | `self.total_tx_size` |
|---|---|
| Before call | `T` |
| After `updated_stat_for_add_tx` | `T` (unchanged; snapshot = `T + new_size`) |
| After eviction of `E` bytes | `T − E` (correctly decremented) |
| After line 218 overwrite | `T + new_size` (**wrong**; should be `T − E + new_size`) |

The evicted transactions' sizes/cycles are silently added back into the totals on every such insertion.

---

### Impact Explanation

`total_tx_size` is the primary guard for the pool's size limit. An inflated value causes two cascading effects:

1. **Spurious eviction**: `limit_size` loops while `self.pool_map.total_tx_size > self.config.max_tx_pool_size`, evicting valid pending/proposed transactions that would otherwise remain in the pool. [6](#0-5) 

2. **Spurious rejection**: `updated_stat_for_add_tx` uses the inflated `self.total_tx_size` as the base for overflow detection, causing subsequent legitimate transactions to be rejected with `Reject::Full` even when the pool has real capacity. [7](#0-6) 

The `total_tx_cycles` inflation has the same effect on cycle-based accounting. The `tx_pool_info` RPC also returns incorrect `total_tx_size`/`total_tx_cycles` values, misleading operators and wallets. [8](#0-7) 

---

### Likelihood Explanation

The eviction path inside `check_and_record_ancestors` is triggered when:
- A new transaction's ancestor count exceeds `max_ancestors_count` (default 25), **and**
- The excess is entirely attributable to cell-dep-referencing parents (`cell_ref_parents`). [9](#0-8) 

An unprivileged transaction sender can craft a transaction that satisfies both conditions: submit a chain of 25+ transactions where the new transaction shares a cell dep with an existing pool transaction. This is reachable via the `send_transaction` RPC or P2P relay (`RelayV3`). No privileged access is required. The inflation accumulates with each such insertion, so repeated submissions amplify the accounting error.

---

### Recommendation

Compute the stat snapshot **after** evictions complete, not before. Move `updated_stat_for_add_tx` to after `check_and_record_ancestors` returns, so it reads the already-decremented `self.total_tx_size`/`self.total_tx_cycles`:

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
    // Move stat computation to AFTER evictions
    evicts = self.check_and_record_ancestors(&mut entry)?;
    self.record_entry_edges(&entry)?;
    // Now compute against the post-eviction totals
    let (total_tx_size, total_tx_cycles) =
        self.updated_stat_for_add_tx(entry.size, entry.cycles)?;
    self.insert_entry(&entry, status);
    self.record_entry_descendants(&entry);
    self.track_entry_statics(None, Some(status));
    self.total_tx_size = total_tx_size;
    self.total_tx_cycles = total_tx_cycles;
    Ok((true, evicts))
}
```

---

### Proof of Concept

1. Fill the pool with a chain of 24 transactions (T1 → T2 → … → T24), all referencing a shared live cell as a cell dep (making them `cell_ref_parents`).
2. Submit T25, which spends T24's output and also references the same shared cell dep. Its ancestor count is 25 = `max_ancestors_count`, so the eviction branch fires: one cell-dep parent is evicted (say T24 + its descendants), reducing ancestor count to 24.
3. After insertion, `total_tx_size` is inflated by the size of the evicted entries.
4. Repeat step 2 with fresh transactions. Each iteration inflates `total_tx_size` further.
5. After enough iterations, `total_tx_size` exceeds `max_tx_pool_size` even though the actual pool is nearly empty. Subsequent `send_transaction` calls are rejected with `Reject::Full`, and `tx_pool_info` reports a grossly inflated `total_tx_size`.

The root cause is entirely within `PoolMap::add_entry` in `tx-pool/src/component/pool_map.rs` at lines 210–219. [10](#0-9)

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

**File:** tx-pool/src/component/pool_map.rs (L246-248)
```rust
            self.track_entry_statics(Some(entry.status), None);
            self.update_stat_for_remove_tx(entry.inner.size, entry.inner.cycles);
            entry.inner
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

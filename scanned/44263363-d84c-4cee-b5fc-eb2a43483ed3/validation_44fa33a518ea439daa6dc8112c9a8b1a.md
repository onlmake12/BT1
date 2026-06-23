### Title
Stale Pre-Eviction `total_tx_size`/`total_tx_cycles` Overwrites Correct Post-Eviction Values in `add_entry` — (`File: tx-pool/src/component/pool_map.rs`)

---

### Summary

In `PoolMap::add_entry`, the new entry's contribution to `total_tx_size` and `total_tx_cycles` is computed **before** any ancestor-eviction occurs. When `check_and_record_ancestors` evicts transactions (correctly decrementing the running totals via `update_stat_for_remove_tx`), those decrements are immediately overwritten by the stale pre-eviction snapshot. The result is that `total_tx_size` and `total_tx_cycles` are **overestimated** by the sum of all evicted transactions' sizes and cycles, causing the pool to incorrectly evict additional legitimate transactions.

---

### Finding Description

In `add_entry` (`tx-pool/src/component/pool_map.rs`, lines 200–221):

```rust
pub(crate) fn add_entry(...) -> Result<(bool, HashSet<TxEntry>), Reject> {
    ...
    // Step 1: snapshot pre-eviction totals + new entry
    let (total_tx_size, total_tx_cycles) =
        self.updated_stat_for_add_tx(entry.size, entry.cycles)?;
    //   ^^ local vars = self.total_tx_size + entry.size
    //                   self.total_tx_cycles + entry.cycles

    // Step 2: may evict N transactions via remove_entry_and_descendants,
    //         each call to remove_entry → update_stat_for_remove_tx
    //         correctly decrements self.total_tx_size and self.total_tx_cycles
    evicts = self.check_and_record_ancestors(&mut entry)?;

    self.insert_entry(&entry, status);
    ...
    // Step 3: OVERWRITES the correctly-updated self.total_tx_size
    //         with the stale pre-eviction snapshot
    self.total_tx_size = total_tx_size;    // BUG: ignores evictions
    self.total_tx_cycles = total_tx_cycles; // BUG: ignores evictions
    Ok((true, evicts))
}
``` [1](#0-0) 

The eviction path is triggered inside `check_and_record_ancestors` when a new transaction's ancestor count exceeds `max_ancestors_count` and there are `cell_ref_parents` that can be evicted: [2](#0-1) 

Each call to `remove_entry_and_descendants` → `remove_entry` correctly decrements `self.total_tx_size` and `self.total_tx_cycles`: [3](#0-2) 

But those decrements are immediately discarded when lines 218–219 write the stale local variables back to `self`.

**Arithmetic illustration:**

| Step | `self.total_tx_size` | Correct value |
|---|---|---|
| Before `add_entry` | `S` | `S` |
| After `updated_stat_for_add_tx` (local only) | `S` | `S` |
| After evicting N txs (total evicted size = `E`) | `S - E` | `S - E` |
| After line 218 overwrites | `S + entry.size` | `S - E + entry.size` |

The overestimate is exactly `E` (the total size of all evicted transactions).

---

### Impact Explanation

`total_tx_size` is the sole guard used by `limit_size()` to decide whether to evict more transactions: [4](#0-3) 

Because `total_tx_size` is overestimated by `E` after the eviction path fires, `limit_size()` will continue evicting additional legitimate transactions that would not have been evicted had the accounting been correct. This causes **legitimate pending/proposed transactions to be silently dropped** from the pool.

The overestimated value is also returned via the `get_tx_pool_info` RPC, causing callers to observe an inflated pool size.

---

### Likelihood Explanation

The eviction path in `check_and_record_ancestors` is triggered when:
1. A new transaction references a cell dep that is already referenced by many in-pool transactions (`cell_ref_parents`), AND
2. The resulting ancestor count exceeds `max_ancestors_count` (default 125).

An unprivileged tx-pool submitter can deliberately craft this scenario: submit many transactions that all reference the same cell dep output, then submit a new transaction that consumes that cell dep. This is a normal, valid transaction pattern. The eviction path fires, the accounting is corrupted, and subsequent `limit_size` calls drop legitimate transactions.

---

### Recommendation

Compute the new totals **after** `check_and_record_ancestors` completes (so evictions are already reflected in `self.total_tx_size`/`self.total_tx_cycles`), then add only the new entry's contribution:

```rust
evicts = self.check_and_record_ancestors(&mut entry)?;
self.record_entry_edges(&entry)?;
self.insert_entry(&entry, status);
self.record_entry_descendants(&entry);
self.track_entry_statics(None, Some(status));
// Correct: add new entry's contribution to already-eviction-adjusted totals
self.total_tx_size = self.total_tx_size.saturating_add(entry.size);
self.total_tx_cycles = self.total_tx_cycles.saturating_add(entry.cycles);
```

Alternatively, move the `updated_stat_for_add_tx` call to after `check_and_record_ancestors`, so the overflow check uses the post-eviction baseline.

---

### Proof of Concept

1. Fill the pool with 2000 transactions that all reference the same cell dep output `O` (each is a `cell_ref_parent`). Pool `total_tx_size = 2000 * S_tx`.
2. Submit a new transaction `T_new` that **consumes** output `O` as an input. This makes all 2000 transactions `cell_ref_parents` of `T_new`.
3. `add_entry` is called for `T_new`:
   - `updated_stat_for_add_tx` snapshots `local_total = 2000*S_tx + S_new`.
   - `check_and_record_ancestors` evicts, say, 1001 transactions (each calling `update_stat_for_remove_tx`), so `self.total_tx_size` drops to `999*S_tx`.
   - Line 218 overwrites: `self.total_tx_size = 2000*S_tx + S_new`.
4. `limit_size()` now sees `total_tx_size = 2000*S_tx + S_new` instead of the correct `1000*S_tx + S_new`, and proceeds to evict the remaining 999 legitimate transactions unnecessarily. [5](#0-4) [6](#0-5) [7](#0-6)

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

**File:** tx-pool/src/component/pool_map.rs (L247-247)
```rust
            self.update_stat_for_remove_tx(entry.inner.size, entry.inner.cycles);
```

**File:** tx-pool/src/component/pool_map.rs (L615-625)
```rust
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

**File:** tx-pool/src/component/pool_map.rs (L710-729)
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
    }
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

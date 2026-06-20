### Title
Stale Pre-Computed `total_tx_size`/`total_tx_cycles` Overwrite in `add_entry` Inflates Pool Size Counter — (File: `tx-pool/src/component/pool_map.rs`)

---

### Summary

In `PoolMap::add_entry`, the new `total_tx_size` and `total_tx_cycles` values are pre-computed **before** `check_and_record_ancestors` executes. When `check_and_record_ancestors` evicts transactions via `remove_entry_and_descendants`, each eviction correctly decrements `self.total_tx_size` and `self.total_tx_cycles` through `update_stat_for_remove_tx`. However, the pre-computed stale values are then unconditionally written back at the end of `add_entry`, overwriting those decrements. This permanently inflates both counters by the aggregate size and cycles of all evicted transactions.

---

### Finding Description

In `add_entry` (`pool_map.rs:200–221`):

```rust
pub(crate) fn add_entry(
    &mut self,
    mut entry: TxEntry,
    status: Status,
) -> Result<(bool, HashSet<TxEntry>), Reject> {
    ...
    // Step 1: pre-compute BEFORE any evictions
    let (total_tx_size, total_tx_cycles) =
        self.updated_stat_for_add_tx(entry.size, entry.cycles)?;   // line 210-211

    // Step 2: may evict transactions, each calling update_stat_for_remove_tx
    //         which decrements self.total_tx_size / self.total_tx_cycles
    evicts = self.check_and_record_ancestors(&mut entry)?;          // line 213

    self.record_entry_edges(&entry)?;
    self.insert_entry(&entry, status);
    self.record_entry_descendants(&entry);
    self.track_entry_statics(None, Some(status));

    // Step 3: stale pre-eviction values overwrite the decrements from Step 2
    self.total_tx_size = total_tx_size;                             // line 218
    self.total_tx_cycles = total_tx_cycles;                         // line 219
    Ok((true, evicts))
}
```

The eviction path inside `check_and_record_ancestors` is triggered when:
1. The new transaction's ancestor count exceeds `max_ancestors_count` (default 25), **and**
2. Some of those ancestors are `cell_ref_parents` (pool transactions referenced as cell deps by the new tx), **and**
3. Removing those `cell_ref_parents` would bring the count within the limit.

When this path fires, `remove_entry_and_descendants` is called for each evicted ancestor, which calls `update_stat_for_remove_tx` to decrement `self.total_tx_size`. But the write-back at lines 218–219 uses the value computed at line 210–211 — before any evictions — so the decrements are silently discarded.

The `update_stat_for_remove_tx` function itself even acknowledges that cycles accounting can be inaccurate:

> `/// cycles overflow is possible, currently obtaining cycles is not accurate` [1](#0-0) 

The pre-computation and stale overwrite: [2](#0-1) 

The eviction path in `check_and_record_ancestors`: [3](#0-2) 

The `update_stat_for_remove_tx` called during eviction (which gets overwritten): [4](#0-3) 

---

### Impact Explanation

After each `add_entry` call that triggers the eviction path, `total_tx_size` is inflated by the aggregate serialized size of all evicted transactions. This inflation is permanent and accumulates across multiple such calls.

`total_tx_size` is the sole gate for `limit_size`:

```rust
while self.pool_map.total_tx_size > self.config.max_tx_pool_size {
``` [5](#0-4) 

An inflated counter causes `limit_size` to over-evict legitimate pending/proposed transactions that should remain in the pool. Additionally, `updated_stat_for_add_tx` uses `total_tx_size` to gate new admissions: [6](#0-5) 

So new transactions are rejected with `Reject::Full` even when the actual pool occupancy is well below `max_tx_pool_size`. The node's tx-pool becomes progressively less useful as the counter drifts further from reality, causing legitimate transactions to be evicted or refused without cause.

---

### Likelihood Explanation

The trigger condition requires a transaction whose ancestor count in the pool exceeds `max_ancestors_count` (default 25) and whose cell deps reference at least one of those ancestors. A tx-pool submitter can deliberately construct this scenario:

1. Submit a chain of 26 transactions T1 → T2 → … → T26 (each spending the previous output).
2. Submit T27 that spends a chain UTXO **and** references T1 as a cell dep. T27 now has 26 ancestors (T1–T26), exceeding the limit of 25, and T1 is a `cell_ref_parent`.
3. The condition `26 - 1 = 25 <= max_ancestors_count` is satisfied, so the eviction path fires: T1 (and its descendants) are evicted, but `total_tx_size` is written back with the pre-eviction value + T27's size.
4. Repeat with fresh UTXOs to accumulate inflation.

This is reachable by any unprivileged tx-pool submitter with no special privileges.

---

### Recommendation

Move the stat pre-computation to **after** `check_and_record_ancestors` completes, so it reflects the post-eviction pool state:

```rust
evicts = self.check_and_record_ancestors(&mut entry)?;
// Re-compute stats after evictions have already decremented the counters
let (total_tx_size, total_tx_cycles

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

**File:** tx-pool/src/component/pool_map.rs (L716-721)
```rust
        let total_tx_size = self.total_tx_size.checked_add(tx_size).ok_or_else(|| {
            Reject::Full(format!(
                "tx-pool total_tx_size {} overflows by add {}",
                self.total_tx_size, tx_size
            ))
        })?;
```

**File:** tx-pool/src/component/pool_map.rs (L731-758)
```rust
    /// Update size and cycles statistics for remove tx
    /// cycles overflow is possible, currently obtaining cycles is not accurate
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

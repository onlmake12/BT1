### Title
Stale Pre-Computed `total_tx_size`/`total_tx_cycles` Overwrites Correct Post-Eviction Values in `add_entry` — (`tx-pool/src/component/pool_map.rs`)

---

### Summary

In `PoolMap::add_entry`, the new `total_tx_size` and `total_tx_cycles` are computed **before** `check_and_record_ancestors` runs. That function may evict transactions (via `remove_entry_and_descendants` → `remove_entry` → `update_stat_for_remove_tx`), which correctly decrements the live counters. However, the stale pre-computed values are then unconditionally written back, silently discarding the eviction decrements. The result is that `total_tx_size` and `total_tx_cycles` become permanently inflated by the aggregate size/cycles of every evicted transaction, causing the pool to believe it is fuller than it actually is and to reject legitimate transactions with `Reject::Full`.

---

### Finding Description

`PoolMap::add_entry` executes the following sequence:

```
// Step 1 – snapshot new totals BEFORE any evictions
let (total_tx_size, total_tx_cycles) =
    self.updated_stat_for_add_tx(entry.size, entry.cycles)?;   // line 210-211

// Step 2 – may call remove_entry_and_descendants → remove_entry
//           → update_stat_for_remove_tx, which DECREMENTS
//           self.total_tx_size and self.total_tx_cycles
evicts = self.check_and_record_ancestors(&mut entry)?;          // line 213

// Step 3 – OVERWRITES the correctly-decremented live counters
//           with the stale snapshot from Step 1
self.total_tx_size  = total_tx_size;                            // line 218
self.total_tx_cycles = total_tx_cycles;                         // line 219
``` [1](#0-0) 

The eviction path inside `check_and_record_ancestors` is triggered when a new transaction's ancestor count exceeds `max_ancestors_count` but the excess is entirely composed of `cell_ref_parents` (pool transactions that reference the same cell dep as the incoming transaction). In that case, the lowest-fee cell_ref_parents are removed via `remove_entry_and_descendants`: [2](#0-1) 

Each call to `remove_entry` correctly decrements `self.total_tx_size` and `self.total_tx_cycles`: [3](#0-2) 

But those decrements are immediately erased when `add_entry` writes back the stale snapshot at lines 218-219. After each such eviction event, `total_tx_size` is inflated by exactly the sum of the evicted transactions' sizes, and `total_tx_cycles` by their cycles.

The inflated counter is then used in `updated_stat_for_add_tx` to gate future insertions: [4](#0-3) 

When `total_tx_size` overflows `usize::MAX` (or the pool's configured `max_tx_pool_size` limit is enforced upstream), every subsequent `add_entry` call returns `Reject::Full`, permanently blocking new transactions from entering the pool.

---

### Impact Explanation

`total_tx_size` and `total_tx_cycles` are the authoritative pool-size counters exposed via `get_tx_pool_info` and used to enforce pool capacity limits. After one or more eviction events, these counters diverge upward from reality. The pool reports and enforces a phantom size larger than the actual bytes/cycles it holds. Legitimate transactions submitted by any user are rejected with `Reject::Full` even though the pool has physical capacity. This is a **tx-pool denial-of-service**: the pool becomes permanently unable to accept new transactions without a node restart.

---

### Likelihood Explanation

The eviction path in `check_and_record_ancestors` is reachable by any unprivileged transaction submitter. An attacker needs only to:

1. Submit a set of transactions that share a common cell dep (making them `cell_ref_parents` of each other).
2. Submit a new transaction whose ancestor count exceeds `max_ancestors_count` but whose excess is covered by those `cell_ref_parents`.

This is a standard, valid transaction submission flow. No privileged keys, no majority hashpower, and no social engineering are required. The condition is documented in the code itself (the `FIXME` comment at line 583 acknowledges that eviction-then-failure rollback is not handled). Repeated triggering accumulates inflation monotonically until the pool is effectively frozen. [5](#0-4) 

---

### Recommendation

Move the stat update to **after** `check_and_record_ancestors` completes, computing the delta relative to the post-eviction live counters rather than pre-computing against the pre-eviction snapshot. Concretely, replace the pre-computed snapshot pattern with an incremental add applied only after all evictions have settled:

```rust
// After check_and_record_ancestors, record_entry_edges, insert_entry, etc.
self.total_tx_size  = self.total_tx_size.saturating_add(entry.size);
self.total_tx_cycles = self.total_tx_cycles.saturating_add(entry.cycles);
```

This ensures that `update_stat_for_remove_tx` decrements applied during eviction are not overwritten.

---

### Proof of Concept

Assume `max_ancestors_count = 25` and the pool already contains 25 transactions `A₁…A₂₅` that all reference cell dep `C` (making them `cell_ref_parents`). Submit a new transaction `T` that:
- Spends an output of `A₁` (making `A₁` a parent/ancestor of `T`)
- Also references cell dep `C`

`check_and_record_ancestors` sees `ancestors_count = 26 > 25`, but `cell_ref_parents = {A₁…A₂₅}`, so `26 - 25 = 1 ≤ 25`. It evicts `A₁` (lowest fee), calling `update_stat_for_remove_tx(A₁.size, A₁.cycles)` which decrements `self.total_tx_size` by `A₁.size`. Then `add_entry` writes back `total_tx_size = old_total + T.size` (the stale snapshot), re-inflating by `A₁.size`. After `N` such submissions, `total_tx_size` is inflated by `N × A.size` bytes above reality. Once the inflated value exceeds `usize::MAX` or the pool's configured size limit, `updated_stat_for_add_tx` returns `Reject::Full` for every subsequent submission, freezing the pool.

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

**File:** tx-pool/src/component/pool_map.rs (L583-587)
```rust
    // FIXME: In the scenario that a transaction passed all RBF rules, and then removed the conflicted
    // transaction in txpool, then failed with max ancestor limits, we now need to rollback the removing.
    // this is not an issue currently, because RBF have a rule that not allow any unknown inputs except
    // the conflicted inputs, so the new transaction can not be in a long transaction chain.
    // but it's still safer to report an error before any writing kind of operation.
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

**File:** tx-pool/src/component/pool_map.rs (L711-728)
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
```

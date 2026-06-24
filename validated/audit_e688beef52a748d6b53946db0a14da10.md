Audit Report

## Title
Stale Pre-Eviction Snapshot Overwrites Correctly-Decremented `total_tx_size`/`total_tx_cycles` in `PoolMap::add_entry` — (File: `tx-pool/src/component/pool_map.rs`)

## Summary
In `PoolMap::add_entry`, `updated_stat_for_add_tx` captures `self.total_tx_size + entry.size` into a local variable **before** `check_and_record_ancestors` runs. When `check_and_record_ancestors` evicts cell-dep parent transactions via `remove_entry_and_descendants` → `update_stat_for_remove_tx`, it correctly decrements `self.total_tx_size`. However, the stale pre-eviction snapshot is then unconditionally written back to `self.total_tx_size`, permanently inflating the counter by the aggregate size of all evicted transactions. An unprivileged attacker can repeat this to drive `total_tx_size` above `max_tx_pool_size`, causing `limit_size` to evict valid transactions and reject new submissions with `Reject::Full`.

## Finding Description
The exact sequence in `add_entry` (L210–220):

```rust
// L210-211: snapshot taken BEFORE evictions
let (total_tx_size, total_tx_cycles) =
    self.updated_stat_for_add_tx(entry.size, entry.cycles)?;

// L213: evictions happen here — remove_entry_and_descendants → update_stat_for_remove_tx
// correctly decrements self.total_tx_size by each evicted tx's size
evicts = self.check_and_record_ancestors(&mut entry)?;

// L218-219: stale snapshot written back, overwriting the decremented value
self.total_tx_size = total_tx_size;
self.total_tx_cycles = total_tx_cycles;
``` [1](#0-0) 

`updated_stat_for_add_tx` (L716) computes `self.total_tx_size.checked_add(tx_size)` as a pure read with no side effects, capturing the pre-eviction value: [2](#0-1) 

`check_and_record_ancestors` (L603–625) enters the eviction branch when `ancestors_count > max_ancestors_count` but `ancestors_count - cell_ref_parents.len() <= max_ancestors_count`, calling `remove_entry_and_descendants` in a loop: [3](#0-2) 

`update_stat_for_remove_tx` (L738–740) correctly decrements `self.total_tx_size` and `self.total_tx_cycles` for each removed entry: [4](#0-3) 

But those decrements are immediately overwritten by the stale snapshot at L218–219. The self-healing path (`recompute_total_stat`) is only triggered on underflow, never on overflow, so the inflation is never corrected. [5](#0-4) 

**Net effect per invocation:**

| Counter | Correct value | Actual value written |
|---|---|---|
| `total_tx_size` | `original − Σ(evicted.size) + entry.size` | `original + entry.size` |
| `total_tx_cycles` | `original − Σ(evicted.cycles) + entry.cycles` | `original + entry.cycles` |

## Impact Explanation
`limit_size` uses `self.pool_map.total_tx_size > self.config.max_tx_pool_size` as its sole eviction trigger: [6](#0-5) 

An inflated `total_tx_size` causes `limit_size` to evict otherwise-valid pending/proposed transactions and reject new valid submissions with `Reject::Full`. Repeated triggering compounds the inflation monotonically. This maps to **High: Vulnerabilities or bad designs which could cause CKB network congestion with few costs** — an attacker can render the tx-pool effectively unusable for all other participants at negligible cost.

## Likelihood Explanation
The trigger condition requires only that a submitted transaction's ancestor count (via cell-dep references) exceeds `max_ancestors_count` but can be reduced to within limits by evicting cell-dep parents. This is reachable by any unprivileged `send_transaction` RPC caller or P2P relay peer. No key material, privileged access, or majority hash power is required. The attack is repeatable and the inflation accumulates across invocations.

## Recommendation
Move `updated_stat_for_add_tx` to **after** `check_and_record_ancestors` completes, so evictions are already reflected in `self.total_tx_size` when the snapshot is taken:

```diff
- let (total_tx_size, total_tx_cycles) =
-     self.updated_stat_for_add_tx(entry.size, entry.cycles)?;
  evicts = self.check_and_record_ancestors(&mut entry)?;
  self.record_entry_edges(&entry)?;
  self.insert_entry(&entry, status);
  self.record_entry_descendants(&entry);
  self.track_entry_statics(None, Some(status));
+ let (total_tx_size, total_tx_cycles) =
+     self.updated_stat_for_add_tx(entry.size, entry.cycles)?;
  self.total_tx_size = total_tx_size;
  self.total_tx_cycles = total_tx_cycles;
```

Note: the overflow check in `updated_stat_for_add_tx` must still be performed; moving it after evictions preserves correctness since evictions can only decrease `self.total_tx_size`, making overflow less likely, not more.

## Proof of Concept
1. Fill the pool with transactions `T1…Tk` where each `Ti` is referenced as a cell dep by `T_{i+1}`, forming a chain of length `max_ancestors_count`. Record `initial_total_tx_size`.
2. Submit `Tnew` referencing `T1` as a cell dep. Its ancestor count is `max_ancestors_count + 1`, satisfying the eviction branch condition at L603.
3. `check_and_record_ancestors` evicts `T1` (and descendants) via `remove_entry_and_descendants`. `self.total_tx_size` is decremented by `Σ(evicted.size)`.
4. `add_entry` writes back the stale snapshot: `self.total_tx_size = initial_total_tx_size + Tnew.size` (ignoring the decrement). Inflation = `Σ(evicted.size)`.
5. Repeat steps 1–4. Each iteration inflates `total_tx_size` by `Σ(evicted.size)`.
6. After enough iterations, `total_tx_size > max_tx_pool_size` even though the actual pool is nearly empty. `limit_size` evicts all remaining transactions; new submissions receive `Reject::Full`.

A unit test can verify this by asserting `pool_map.total_tx_size == actual_sum_of_entry_sizes` after a sequence of add-with-eviction operations.

### Citations

**File:** tx-pool/src/component/pool_map.rs (L210-220)
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
        Ok((true, evicts))
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

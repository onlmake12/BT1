Audit Report

## Title
`total_tx_size`/`total_tx_cycles` Overwritten With Stale Pre-Eviction Snapshot in `add_entry` — (`tx-pool/src/component/pool_map.rs`)

## Summary

In `PoolMap::add_entry`, the new pool-size totals are computed as a local snapshot before `check_and_record_ancestors` runs. That inner call can evict existing pool entries via `remove_entry` → `update_stat_for_remove_tx`, which correctly decrements `self.total_tx_size`/`self.total_tx_cycles` in place. Immediately afterward, `add_entry` unconditionally overwrites those fields with the stale pre-eviction snapshot, silently re-inflating the totals by the aggregate size and cycles of every evicted transaction. The corruption accumulates across repeated invocations and causes `limit_size` to evict legitimate transactions and `tx_pool_info` to report a pool larger than reality.

## Finding Description

**Root cause — `add_entry` (lines 200–221):**

```
// Step 1: snapshot = (self.total_tx_size + entry.size, self.total_tx_cycles + entry.cycles)
let (total_tx_size, total_tx_cycles) =
    self.updated_stat_for_add_tx(entry.size, entry.cycles)?;   // pure read, no mutation

// Step 2: may call remove_entry_and_descendants → remove_entry → update_stat_for_remove_tx
//         which DOES mutate self.total_tx_size / self.total_tx_cycles
evicts = self.check_and_record_ancestors(&mut entry)?;

// Step 3: OVERWRITE with the stale snapshot — evictions are silently undone
self.total_tx_size = total_tx_size;
self.total_tx_cycles = total_tx_cycles;
```

`updated_stat_for_add_tx` (lines 711–729) is a pure `&self` method returning `(self.total_tx_size + tx_size, self.total_tx_cycles + cycles)` without touching `self`. [1](#0-0) 

`remove_entry` (lines 235–250) calls `update_stat_for_remove_tx` at line 247, which correctly decrements `self.total_tx_size` and `self.total_tx_cycles` in place. [2](#0-1) 

The eviction path inside `check_and_record_ancestors` (lines 603–625) is entered when `ancestors_count - cell_ref_parents.len() <= max_ancestors_count`, and calls `remove_entry_and_descendants` for each cell-ref parent that must be evicted. [3](#0-2) 

After N evictions, the correct value of `total_tx_size` is `original − Σ(evicted[i].size) + entry.size`. Step 3 writes `original + entry.size`, permanently re-adding `Σ(evicted[i].size)` as phantom bytes. The overwrite at lines 218–219 is the exact site of the corruption. [4](#0-3) 

## Impact Explanation

`limit_size` loops while `self.pool_map.total_tx_size > self.config.max_tx_pool_size` and evicts the lowest-fee pending entries. [5](#0-4)  An inflated `total_tx_size` causes this loop to evict real, legitimate transactions to compensate for phantom bytes. An attacker who repeatedly triggers the eviction path can accumulate enough phantom inflation to continuously purge targeted transactions from the mempool of any node that accepts their crafted transaction chain. Because CKB nodes propagate transactions over P2P, the attack can be replicated across the network with minimal cost, causing sustained mempool disruption and preventing legitimate transactions from being confirmed. This matches the **High** impact class: *Vulnerabilities or bad designs which could cause CKB network congestion with few costs.*

## Likelihood Explanation

The eviction path requires only that the incoming transaction has more than `max_ancestors_count` (default 25) ancestors, but the excess is entirely attributable to cell-ref parents. Any unprivileged RPC caller or P2P sender can construct this scenario:

1. Submit a chain T₁ → T₂ → … → T₂₆ (26 dependent transactions).
2. Submit D₁, D₂, D₃ each referencing T₁'s output as a **cell dep** (making them cell-ref parents).
3. Submit T_new spending T₂₆'s output (26 ancestors) and also spending T₁'s output directly. `ancestors_count = 27 > 25`; `cell_ref_parents = {D₁, D₂, D₃}`; `27 − 3 = 24 ≤ 25` → eviction path entered.

No privileged access, no victim mistakes, and no external dependencies are required. The attack is repeatable and the inflation is cumulative.

## Recommendation

Move the accounting snapshot **after** `check_and_record_ancestors` completes so it reflects the post-eviction state of `self.total_tx_size`/`self.total_tx_cycles`:

```rust
// Validate capacity (overflow check only) — do NOT capture the snapshot yet
self.updated_stat_for_add_tx(entry.size, entry.cycles)?;

let evicts = self.check_and_record_ancestors(&mut entry)?;
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

Alternatively, replace the snapshot pattern entirely with direct in-place increments (`self.total_tx_size += entry.size`) applied only after all evictions are complete, eliminating the stale-snapshot hazard.

## Proof of Concept

1. Submit T₁ → T₂ → … → T₂₆ (chain of 26 transactions, each spending the previous output).
2. Submit D₁, D₂, D₃ each using T₁'s output as a cell dep.
3. Submit T_new spending T₂₆'s output and T₁'s output directly.
4. `check_and_record_ancestors` evicts D₁, D₂, D₃ via `remove_entry_and_descendants`, correctly decrementing `self.total_tx_size` by `size(D₁) + size(D₂) + size(D₃)`.
5. Lines 218–219 overwrite `self.total_tx_size` with the pre-eviction snapshot, re-adding `size(D₁) + size(D₂) + size(D₃)` as phantom bytes.
6. Query `tx_pool_info` RPC and observe `total_tx_size` is inflated by `size(D₁) + size(D₂) + size(D₃)`.
7. Repeat steps 1–6 to accumulate inflation until `limit_size` begins evicting legitimate transactions.

A unit test can assert that after a successful `add_entry` that triggers the eviction path, `pool_map.total_tx_size` equals the sum of sizes of all entries actually present in `pool_map.entries`, confirming the invariant is violated without the fix.

### Citations

**File:** tx-pool/src/component/pool_map.rs (L218-219)
```rust
        self.total_tx_size = total_tx_size;
        self.total_tx_cycles = total_tx_cycles;
```

**File:** tx-pool/src/component/pool_map.rs (L235-250)
```rust
    pub(crate) fn remove_entry(&mut self, id: &ProposalShortId) -> Option<TxEntry> {
        self.entries.remove_by_id(id).map(|entry| {
            debug!(
                "remove entry {} from status: {:?}",
                entry.inner.transaction().hash(),
                entry.status
            );
            self.update_ancestors_index_key(&entry.inner, EntryOp::Remove);
            self.update_descendants_index_key(&entry.inner, EntryOp::Remove);
            self.remove_entry_edges(&entry.inner);
            self.remove_entry_links(id);
            self.track_entry_statics(Some(entry.status), None);
            self.update_stat_for_remove_tx(entry.inner.size, entry.inner.cycles);
            entry.inner
        })
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

**File:** tx-pool/src/pool.rs (L292-299)
```rust
    pub(crate) fn limit_size(
        &mut self,
        callbacks: &Callbacks,
        current_entry_id: Option<&ProposalShortId>,
    ) -> Option<Reject> {
        let mut ret = None;
        while self.pool_map.total_tx_size > self.config.max_tx_pool_size {
            let next_evict_entry = || {
```

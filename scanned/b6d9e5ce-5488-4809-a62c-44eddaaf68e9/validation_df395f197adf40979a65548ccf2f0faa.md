Audit Report

## Title
Stale Pre-Eviction Snapshot Overwrites Live `total_tx_size`/`total_tx_cycles` After Ancestor Eviction — (File: tx-pool/src/component/pool_map.rs)

## Summary
In `PoolMap::add_entry`, `updated_stat_for_add_tx` snapshots `self.total_tx_size + entry.size` before `check_and_record_ancestors` runs. That function can evict existing pool entries via `remove_entry_and_descendants`, each of which calls `update_stat_for_remove_tx` and correctly decrements the live fields. Immediately afterward, `add_entry` blindly overwrites those live fields with the stale pre-eviction snapshot, silently re-inflating the totals by the combined size and cycles of every evicted transaction. An unprivileged attacker can exploit this cheaply and repeatedly to make `total_tx_size` diverge arbitrarily from the real pool size, causing `limit_size` to evict legitimate pending transactions and reject new submissions with `Reject::Full` even when the pool has real capacity.

## Finding Description

**Root cause — ordering in `add_entry` (lines 210–219):**

```
L210-211: (total_tx_size, total_tx_cycles) = updated_stat_for_add_tx(entry.size, entry.cycles)
          // snapshot = self.total_tx_size + entry.size  (pre-eviction)

L213:     evicts = check_and_record_ancestors(&mut entry)
          // may call remove_entry_and_descendants → remove_entry → update_stat_for_remove_tx
          // which DECREMENTS self.total_tx_size / self.total_tx_cycles for each evicted tx

L218-219: self.total_tx_size  = total_tx_size   // OVERWRITES the decremented live value
          self.total_tx_cycles = total_tx_cycles // OVERWRITES the decremented live value
``` [1](#0-0) 

**Eviction path in `check_and_record_ancestors` (lines 603–625):** When a new transaction's ancestor count exceeds `max_ancestors_count` but the excess is attributable to `cell_ref_parents`, the function evicts those lower-fee parents via `remove_entry_and_descendants`. [2](#0-1) 

**`remove_entry_and_descendants` → `remove_entry` → `update_stat_for_remove_tx`:** Each evicted entry correctly decrements `self.total_tx_size` and `self.total_tx_cycles`. [3](#0-2) 

**The stale overwrite:** After all evictions have correctly decremented the live fields, lines 218–219 restore them to the pre-eviction snapshot, re-adding the size/cycles of every evicted transaction. [4](#0-3) 

**`updated_stat_for_add_tx` only checks arithmetic overflow**, not the pool size limit, so it provides no guard against this inflation. [5](#0-4) 

## Impact Explanation

`total_tx_size` is the sole guard in `limit_size` that decides whether to evict further transactions: [6](#0-5) 

An inflated `total_tx_size` causes `limit_size` to keep evicting legitimate pending transactions even though the pool is actually below `max_tx_pool_size`. Each subsequent `add_entry` that triggers the ancestor-eviction path compounds the inflation. Over time the pool's reported size diverges from its real size, causing:

- Legitimate transactions to be evicted unnecessarily.
- The pool to reject new submissions with `Reject::Full` even when real capacity exists.
- Fee-rate estimation to operate on a pool that appears larger than it is, skewing returned fee rates upward.

This matches the **High** impact class: *Vulnerabilities or bad designs which could cause CKB network congestion with few costs.*

## Likelihood Explanation

The trigger requires only standard `send_transaction` RPC calls, open to any node peer or RPC caller:

1. Submit a chain of `max_ancestors_count − 1` (default 24) transactions `T1 → T2 → … → T24`.
2. Submit `T25` whose cell-dep references `T1`, pushing ancestor count to 26 > 25 with `cell_ref_parents = {T1}`.

`remove_entry_and_descendants(T1)` removes T1 and all its descendants (T2…T24), decrementing `total_tx_size` by ~24 KB. The stale snapshot then overwrites it with ~25 KB, leaving an inflation of ~24 KB per iteration while the pool actually holds only T25 (~1 KB).

**Cost estimate:** To inflate `total_tx_size` from 0 to 180 MB requires ≈7,500 iterations × 25 transactions = ~187,500 transactions. At the minimum fee rate of 1,000 shannons/KB and ~1 KB per transaction, the total cost is ~187,500,000 shannons ≈ 1.875 CKB — negligible. No privileged access, key material, or majority hash power is required.

## Recommendation

Move the computation of `total_tx_size`/`total_tx_cycles` to **after** `check_and_record_ancestors` completes, so any evictions are already reflected in `self.total_tx_size`/`self.total_tx_cycles` before the new entry's contribution is added:

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
    // Remove early snapshot — do NOT call updated_stat_for_add_tx here
    trace!("pool_map.add_{:?} {}", status, entry.transaction().hash());
    evicts = self.check_and_record_ancestors(&mut entry)?;
    self.record_entry_edges(&entry)?;
    self.insert_entry(&entry, status);
    self.record_entry_descendants(&entry);
    self.track_entry_statics(None, Some(status));
    // Compute totals AFTER evictions have already updated self.total_tx_*:
    let (total_tx_size, total_tx_cycles) =
        self.updated_stat_for_add_tx(entry.size, entry.cycles)?;
    self.total_tx_size = total_tx_size;
    self.total_tx_cycles = total_tx_cycles;
    Ok((true, evicts))
}
```

Alternatively, call `recompute_total_stat()` unconditionally at the end of `add_entry` whenever `evicts` is non-empty, as the fallback path in `update_stat_for_remove_tx` already demonstrates this is a known recovery mechanism. [7](#0-6) 

## Proof of Concept

**Setup:** `max_tx_pool_size = 180 MB`, `max_ancestors_count = 25`, each transaction ~1 KB.

**Single iteration:**
1. Submit `T1 → T2 → … → T24` (24 chained txs). Pool: `total_tx_size = 24 KB` (correct).
2. Submit `T25` with cell-dep referencing `T1`. Ancestor count = 26 > 25; `cell_ref_parents = {T1}`.
3. `check_and_record_ancestors` calls `remove_entry_and_descendants(T1)`, removing T1…T24. `update_stat_for_remove_tx` is called 24 times: `self.total_tx_size → 0 KB`.
4. `add_entry` overwrites: `self.total_tx_size = 25 KB` (stale snapshot).
5. Pool holds only T25 (~1 KB) but reports `total_tx_size = 25 KB`. **Inflation = 24 KB.**

**Compounding:** Repeat with fresh transaction chains. After N iterations, `total_tx_size` reports `N × 25 KB` while actual pool size is `N × 1 KB`. After ~7,500 iterations (~1.875 CKB in fees), `total_tx_size` reaches 180 MB, causing `limit_size` to begin evicting legitimate transactions from an effectively empty pool.

**Verification test:** An integration test can assert `pool_map.total_tx_size == pool_map.recompute_total_stat().0` after each `add_entry` call that returns non-empty evicts. The bug causes this invariant to fail after the first such call.

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

**File:** tx-pool/src/component/pool_map.rs (L235-248)
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

**File:** tx-pool/src/component/pool_map.rs (L743-749)
```rust
                if let Some((total_tx_size, total_tx_cycles)) = self.recompute_total_stat() {
                    error!(
                        "tx-pool total stats underflowed when removing size {} cycles {}, recomputed size {} cycles {}",
                        tx_size, cycles, total_tx_size, total_tx_cycles
                    );
                    self.total_tx_size = total_tx_size;
                    self.total_tx_cycles = total_tx_cycles;
```

**File:** tx-pool/src/pool.rs (L298-298)
```rust
        while self.pool_map.total_tx_size > self.config.max_tx_pool_size {
```

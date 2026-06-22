### Title
Stale `total_tx_size`/`total_tx_cycles` Overwrite After Cell-Ref Eviction Inflates Pool Accounting — (`File: tx-pool/src/component/pool_map.rs`)

---

### Summary

In `PoolMap::add_entry`, the pool pre-computes new `total_tx_size` and `total_tx_cycles` values **before** calling `check_and_record_ancestors`. That inner call can evict existing cell-ref-parent transactions, each of which correctly decrements `self.total_tx_size` and `self.total_tx_cycles` via `update_stat_for_remove_tx`. However, `add_entry` then **overwrites** those fields with the stale pre-computed values, silently discarding every decrement. The pool permanently believes it is larger than it actually is, causing `limit_size` to evict legitimate transactions and `updated_stat_for_add_tx` to reject new submissions as `Full` even when real capacity exists.

---

### Finding Description

`PoolMap::add_entry` executes the following sequence:

```
// Step 1 – snapshot pre-eviction totals into locals
let (total_tx_size, total_tx_cycles) =
    self.updated_stat_for_add_tx(entry.size, entry.cycles)?;   // line 210-211

// Step 2 – may evict cell-ref parents, each calling update_stat_for_remove_tx
//          which DECREMENTS self.total_tx_size / self.total_tx_cycles
evicts = self.check_and_record_ancestors(&mut entry)?;          // line 213

// Step 3 – OVERWRITES self.total_tx_size with the stale pre-eviction value
self.total_tx_size  = total_tx_size;                            // line 218
self.total_tx_cycles = total_tx_cycles;                         // line 219
``` [1](#0-0) 

`updated_stat_for_add_tx` only reads `self.total_tx_size` and returns a local copy incremented by the new entry's size; it does not modify the field. [2](#0-1) 

`check_and_record_ancestors` enters the eviction branch when `ancestors_count > max_ancestors_count` but `ancestors_count - cell_ref_parents.len() <= max_ancestors_count`. It calls `remove_entry_and_descendants` for each cell-ref parent to be evicted. [3](#0-2) 

`remove_entry_and_descendants` → `remove_entry` → `update_stat_for_remove_tx` correctly decrements `self.total_tx_size` and `self.total_tx_cycles` for every removed entry. [4](#0-3) [5](#0-4) 

After `check_and_record_ancestors` returns, `self.total_tx_size` has been correctly decremented by the total size of all evicted entries (`E`). The assignment on line 218 then restores it to `S + new_size` (where `S` was the pre-eviction value), instead of the correct `S − E + new_size`. The pool's accounting is permanently inflated by `E` bytes and `E_cycles` cycles per triggering submission.

---

### Impact Explanation

`limit_size` drives eviction decisions solely from `self.pool_map.total_tx_size`:

```rust
while self.pool_map.total_tx_size > self.config.max_tx_pool_size {
    // evict lowest-fee entries
}
``` [6](#0-5) 

An inflated `total_tx_size` causes `limit_size` to evict legitimate, fee-paying transactions that would otherwise remain in the pool. Simultaneously, `updated_stat_for_add_tx` rejects new submissions with `Reject::Full` even when real pool capacity is available, because it compares against the inflated counter. The pool's effective capacity shrinks with each triggering submission, eventually reaching a state where all new transactions are rejected as `Full` despite the pool being physically under-capacity.

---

### Likelihood Explanation

The eviction path in `check_and_record_ancestors` is reachable by any unprivileged caller of the `send_transaction` RPC. The attacker must:

1. Submit many transactions that all reference the same cell dep (making them cell-ref parents of a future transaction). The default `max_ancestors_count` is 1000, so up to ~1000 such transactions can be staged.
2. Submit one transaction that **consumes** that cell dep as an input. This triggers the eviction branch, inflating `total_tx_size` by the combined size of the evicted cell-ref parents.
3. Repeat with fresh cell deps.

No privileged access, no key material, no majority hashpower, and no Sybil attack is required. The `send_transaction` RPC is publicly accessible. The existing FIXME comment at line 583 confirms the developers are aware that `check_and_record_ancestors` performs irreversible mutations before all checks pass, but the accounting overwrite on lines 218-219 is a separate, unacknowledged bug. [7](#0-6) 

---

### Recommendation

Compute the final statistics **after** all mutations (including evictions) have completed, rather than pre-computing them before `check_and_record_ancestors`. The simplest correct fix is to move the stat update to after the eviction loop:

```rust
// After all mutations succeed:
self.total_tx_size  = self.total_tx_size.checked_add(entry.size)
    .expect("already checked above");
self.total_tx_cycles = self.total_tx_cycles.checked_add(entry.cycles)
    .expect("already checked above");
```

Alternatively, subtract the evicted entries' sizes from the pre-computed locals before assigning:

```rust
let evict_size:  usize = evicts.iter().map(|e| e.size).sum();
let evict_cycles: Cycle = evicts.iter().map(|e| e.cycles).sum();
self.total_tx_size  = total_tx_size  - evict_size;
self.total_tx_cycles = total_tx_cycles - evict_cycles;
```

---

### Proof of Concept

**Setup** (assume `max_tx_pool_size = 200_000 bytes`, `max_ancestors_count = 1000`):

1. Submit a root transaction `T0` whose output cell is used as a cell dep.
2. Submit 1001 transactions `T1…T1001`, each referencing `T0`'s output as a cell dep. All are accepted; `total_tx_size ≈ 1001 × avg_size`.
3. Submit transaction `T_consume` that spends `T0`'s output as an **input**. Inside `add_entry`:
   - `updated_stat_for_add_tx` snapshots `total_tx_size_local = total_tx_size + size(T_consume)`.
   - `check_and_record_ancestors` detects `ancestors_count = 1002 > 1000`; `cell_ref_parents = {T1…T1001}`. It evicts the lowest-fee cell-ref parents (say 2 entries, total size `E`), calling `update_stat_for_remove_tx` twice → `self.total_tx_size -= E`.
   - Line 218 assigns `self.total_tx_size = total_tx_size_local` (the pre-eviction snapshot + `size(T_consume)`), discarding the `−E` correction.
   - Net inflation: `E` bytes.
4. Repeat steps 1–3 with fresh cell deps. Each iteration inflates `total_tx_size` by `E`.
5. After enough iterations, `total_tx_size > max_tx_pool_size` even though the pool holds far fewer bytes. `limit_size` begins evicting legitimate transactions; `updated_stat_for_add_tx` rejects all new submissions with `Reject::Full`.

The attacker achieves a sustained tx-pool denial-of-service reachable via the public `send_transaction` RPC with no special privileges. [8](#0-7) [9](#0-8) [10](#0-9)

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

**File:** tx-pool/src/component/pool_map.rs (L583-587)
```rust
    // FIXME: In the scenario that a transaction passed all RBF rules, and then removed the conflicted
    // transaction in txpool, then failed with max ancestor limits, we now need to rollback the removing.
    // this is not an issue currently, because RBF have a rule that not allow any unknown inputs except
    // the conflicted inputs, so the new transaction can not be in a long transaction chain.
    // but it's still safer to report an error before any writing kind of operation.
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

### Title
`total_tx_size`/`total_tx_cycles` Overreported After Ancestor-Eviction in `add_entry` — (`File: tx-pool/src/component/pool_map.rs`)

---

### Summary

In `PoolMap::add_entry`, the new transaction's size and cycles are added to running totals **before** ancestor-eviction runs. When `check_and_record_ancestors` evicts existing transactions (calling `update_stat_for_remove_tx` to subtract their sizes/cycles), those subtractions are immediately overwritten by the stale pre-eviction totals. The result is that `total_tx_size` and `total_tx_cycles` are permanently inflated by the size/cycles of every evicted transaction, causing the pool to believe it is larger than it actually is.

---

### Finding Description

In `add_entry` (`tx-pool/src/component/pool_map.rs`, lines 200–221):

```rust
pub(crate) fn add_entry(...) -> Result<(bool, HashSet<TxEntry>), Reject> {
    ...
    // Step 1: compute new totals into LOCAL variables (self.total_tx_size unchanged)
    let (total_tx_size, total_tx_cycles) =
        self.updated_stat_for_add_tx(entry.size, entry.cycles)?;

    // Step 2: may call remove_entry_and_descendants → remove_entry →
    //         update_stat_for_remove_tx, which MODIFIES self.total_tx_size
    evicts = self.check_and_record_ancestors(&mut entry)?;

    ...
    // Step 3: OVERWRITES self.total_tx_size with the stale pre-eviction value
    self.total_tx_size = total_tx_size;
    self.total_tx_cycles = total_tx_cycles;
    Ok((true, evicts))
}
``` [1](#0-0) 

The execution order is:

| Step | `self.total_tx_size` | local `total_tx_size` |
|------|----------------------|-----------------------|
| Before call | `X` | — |
| After `updated_stat_for_add_tx` | `X` (unchanged) | `X + new_size` |
| After `check_and_record_ancestors` evicts S bytes | `X − S` | `X + new_size` (stale) |
| After `self.total_tx_size = total_tx_size` | `X + new_size` (**wrong**) | — |

Correct value should be `X − S + new_size`. The evicted bytes `S` are never subtracted from the final committed total.

The eviction path inside `check_and_record_ancestors` is triggered when a new transaction's ancestor count exceeds `max_ancestors_count` but can be reduced by evicting `cell_ref_parents`: [2](#0-1) 

`remove_entry_and_descendants` correctly calls `update_stat_for_remove_tx` per removed entry: [3](#0-2) 

But those subtractions are immediately clobbered at lines 218–219.

---

### Impact Explanation

`total_tx_size` is the sole guard used by `limit_size()` to enforce the pool's memory cap: [4](#0-3) 

Because `total_tx_size` is inflated by the size of every evicted transaction, `limit_size()` will see the pool as over-capacity even when it is not, and will evict additional valid transactions. Each subsequent submission that triggers the ancestor-eviction path compounds the inflation. In the worst case, a sustained stream of such transactions can drain the mempool of legitimate pending transactions, preventing them from being proposed or committed. The `tx_pool_info` RPC also exposes the corrupted counters: [5](#0-4) 

---

### Likelihood Explanation

The trigger condition — `ancestors_count > max_ancestors_count` while `ancestors_count − cell_ref_parents.len() ≤ max_ancestors_count` — requires a transaction whose cell-dep parents form a long chain already in the pool. Any unprivileged tx-pool submitter can craft this scenario by first seeding the pool with a chain of transactions that share a cell dep, then submitting a transaction that references that dep. No privileged access, key material, or majority hashpower is required. The default `max_ancestors_count` is 25, a chain depth easily reachable on a live node.

---

### Recommendation

Move `updated_stat_for_add_tx` to **after** `check_and_record_ancestors` completes, so the addition is applied to the already-corrected `self.total_tx_size`:

```rust
evicts = self.check_and_record_ancestors(&mut entry)?;
// Now self.total_tx_size reflects evictions; add the new tx on top
let (total_tx_size, total_tx_cycles) =
    self.updated_stat_for_add_tx(entry.size, entry.cycles)?;
...
self.total_tx_size = total_tx_size;
self.total_tx_cycles = total_tx_cycles;
```

Alternatively, recompute via `recompute_total_stat()` after all mutations, though that is O(n) in pool size.

---

### Proof of Concept

1. Fill the pool with a chain of 24 transactions `T1 → T2 → … → T24` where each `Ti` uses the output of `T(i-1)` as a cell dep (making them `cell_ref_parents` of a future transaction).
2. Submit transaction `T25` that references the output of `T24` as a cell dep. Now `ancestors_count = 25 = max_ancestors_count`, so no eviction yet.
3. Submit transaction `T26` that also references `T24`'s output as a cell dep. Now `ancestors_count = 26 > max_ancestors_count`, but `26 − 1 = 25 ≤ max_ancestors_count`, so `check_and_record_ancestors` evicts `T25` (size `S`).
   - `update_stat_for_remove_tx(S, cycles_S)` runs → `self.total_tx_size -= S`.
   - Then `self.total_tx_size = total_tx_size` (pre-eviction value) → `self.total_tx_size` is inflated by `S`.
4. Observe via `tx_pool_info` RPC that `total_tx_size` is larger than the sum of actual entries. Observe that `limit_size()` now evicts additional valid transactions to compensate for the phantom inflation.

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

**File:** tx-pool/src/component/pool_map.rs (L247-248)
```rust
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

**File:** tx-pool/src/service.rs (L1089-1090)
```rust
            total_tx_size: tx_pool.pool_map.total_tx_size,
            total_tx_cycles: tx_pool.pool_map.total_tx_cycles,
```

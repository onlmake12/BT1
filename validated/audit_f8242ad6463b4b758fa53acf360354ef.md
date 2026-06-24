Audit Report

## Title
`add_entry` Overwrites Post-Eviction `total_tx_size`/`total_tx_cycles` with Stale Pre-Eviction Values - (File: tx-pool/src/component/pool_map.rs)

## Summary
In `PoolMap::add_entry`, `updated_stat_for_add_tx` snapshots the new totals from the current (pre-eviction) counters at lines 210–211. When `check_and_record_ancestors` subsequently evicts entries via `remove_entry_and_descendants` → `remove_entry` → `update_stat_for_remove_tx`, those evictions correctly decrement `self.total_tx_size` and `self.total_tx_cycles`. Lines 218–219 then unconditionally overwrite those correctly-updated fields with the stale pre-eviction snapshot, permanently inflating both counters by the aggregate size and cycles of all evicted transactions. Any unprivileged user can trigger this path via the public `send_transaction` RPC, and the inflation compounds with each triggering call.

## Finding Description
The exact sequence in `add_entry` (lines 200–221 of `tx-pool/src/component/pool_map.rs`):

```
L210-211: let (total_tx_size, total_tx_cycles) =
              self.updated_stat_for_add_tx(entry.size, entry.cycles)?;
              // Snapshots: (self.total_tx_size + entry.size, self.total_tx_cycles + entry.cycles)

L213:     evicts = self.check_and_record_ancestors(&mut entry)?;
              // May call remove_entry_and_descendants → remove_entry →
              // update_stat_for_remove_tx (L247), which correctly does:
              //   self.total_tx_size -= evicted_size
              //   self.total_tx_cycles -= evicted_cycles

L218-219: self.total_tx_size = total_tx_size;   // OVERWRITES correct post-eviction value
          self.total_tx_cycles = total_tx_cycles; // with stale pre-eviction snapshot
```

The eviction path inside `check_and_record_ancestors` (lines 603–625) is reached when `ancestors_count > max_ancestors_count` but `ancestors_count - cell_ref_parents.len() <= max_ancestors_count`. In that branch, `remove_entry_and_descendants` is called in a loop, each call invoking `update_stat_for_remove_tx` (line 247) which correctly subtracts from `self.total_tx_size`/`self.total_tx_cycles`. After the loop, `self.total_tx_size` correctly equals `original − Σ(evicted_size)`. Lines 218–219 then restore it to `original + entry.size`, discarding all eviction subtractions. The net inflation per triggering call is exactly `Σ(evicted_size)` and `Σ(evicted_cycles)`.

`updated_stat_for_add_tx` (lines 711–729) only performs overflow checking and returns a snapshot — it does not mutate `self`. `update_stat_for_remove_tx` (lines 733–758) does mutate `self` and is the correct decrement path. The overwrite at lines 218–219 is unconditional and has no guard for the eviction case.

## Impact Explanation
**High — Vulnerabilities or bad designs which could cause CKB network congestion with few costs.**

`limit_size` in `tx-pool/src/pool.rs` (line 298) loops `while self.pool_map.total_tx_size > self.config.max_tx_pool_size` and evicts lowest-fee-rate entries until the condition is false. An inflated `total_tx_size` causes this loop to evict additional legitimate third-party transactions that would otherwise fit. Each eviction correctly decrements the counter via `update_stat_for_remove_tx`, so the loop terminates — but only after having expelled legitimate transactions proportional to the inflation. An attacker can repeat the triggering pattern to continuously displace legitimate transactions from the mempool, preventing their inclusion in blocks and forcing users to resubmit, which constitutes sustained low-cost network congestion. Additionally, `tx_pool_info` in `tx-pool/src/service.rs` (lines 1089–1090) reads `total_tx_size` and `total_tx_cycles` directly, so all RPC consumers receive incorrect pool accounting data.

## Likelihood Explanation
The trigger requires submitting a transaction whose ancestor count exceeds `max_ancestors_count` where the excess is entirely composed of `cell_ref_parents`. This is reachable by any user with access to the public `send_transaction` JSON-RPC endpoint: build a chain of transactions where earlier transactions are referenced as cell deps by later ones, then submit a transaction that pushes the ancestor count over the limit. No privileged role, leaked key, or majority hashpower is required. The condition is fully constructible on mainnet and the attack is repeatable at low cost.

## Recommendation
Move the stat update to after the eviction step, operating on the already-correct post-eviction counters:

```rust
// Remove the pre-eviction snapshot entirely.
// Validate overflow pre-check only (discard returned values):
self.updated_stat_for_add_tx(entry.size, entry.cycles)?;

let evicts = self.check_and_record_ancestors(&mut entry)?;
self.record_entry_edges(&entry)?;
self.insert_entry(&entry, status);
self.record_entry_descendants(&entry);
self.track_entry_statics(None, Some(status));

// Apply the addition AFTER evictions have already adjusted the counters:
self.total_tx_size = self.total_tx_size.saturating_add(entry.size);
self.total_tx_cycles = self.total_tx_cycles.saturating_add(entry.cycles);
```

Alternatively, call `recompute_total_stat` (lines 698–708) after the eviction step, consistent with the underflow-recovery pattern already present in `update_stat_for_remove_tx`.

## Proof of Concept
1. Set `max_ancestors_count = 25`, `max_tx_pool_size = 20 MB`.
2. Submit 23 transactions `tx_1 … tx_23` in a spend chain (each spends an output of the previous).
3. Submit `tx_A` that spends an output of `tx_23` (ancestor count = 24).
4. Submit `tx_B` that spends an output of `tx_A` AND references `tx_A` as a cell dep (making `tx_A` a `cell_ref_parent`; ancestor count = 25, still within limit).
5. Submit `tx_C` that spends an output of `tx_B` AND references `tx_A` as a cell dep. Ancestor count = 26 > 25; `cell_ref_parents = {tx_A}`; `26 − 1 = 25 <= 25`, so the eviction branch is entered.
6. `remove_entry_and_descendants(tx_A)` removes `tx_A` and `tx_B`, correctly decrementing `self.total_tx_size` by `size(tx_A) + size(tx_B)`.
7. Lines 218–219 overwrite `self.total_tx_size` with the pre-eviction snapshot, inflating it by `size(tx_A) + size(tx_B)`.
8. `limit_size` is called; the inflated counter exceeds `max_tx_pool_size` threshold sooner than it should, evicting legitimate third-party transactions from the pool.
9. Repeat steps 2–8 to compound the inflation and continuously displace legitimate transactions. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4)

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

**File:** tx-pool/src/component/pool_map.rs (L244-247)
```rust
            self.remove_entry_edges(&entry.inner);
            self.remove_entry_links(id);
            self.track_entry_statics(Some(entry.status), None);
            self.update_stat_for_remove_tx(entry.inner.size, entry.inner.cycles);
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

**File:** tx-pool/src/pool.rs (L297-298)
```rust
        let mut ret = None;
        while self.pool_map.total_tx_size > self.config.max_tx_pool_size {
```

**File:** tx-pool/src/service.rs (L1089-1090)
```rust
            total_tx_size: tx_pool.pool_map.total_tx_size,
            total_tx_cycles: tx_pool.pool_map.total_tx_cycles,
```

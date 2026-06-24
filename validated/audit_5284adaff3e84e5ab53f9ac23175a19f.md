The code confirms the claim exactly. All three key behaviors are verified:

1. `add_entry` lines 210–211: `updated_stat_for_add_tx` is an immutable `&self` snapshot taken before eviction. [1](#0-0) 

2. `check_and_record_ancestors` line 618: calls `remove_entry_and_descendants` → `remove_entry` → `update_stat_for_remove_tx`, which directly mutates `self.total_tx_size`/`self.total_tx_cycles` in-place. [2](#0-1) 

3. Lines 218–219 unconditionally overwrite those fields with the stale pre-eviction snapshot. [3](#0-2) 

4. `limit_size` loops `while self.pool_map.total_tx_size > self.config.max_tx_pool_size`, so the inflated value triggers cascading eviction. [4](#0-3) 

---

Audit Report

## Title
Stale Pre-Computed `total_tx_size`/`total_tx_cycles` Snapshot Overwrites In-Place Eviction Updates in `add_entry` — (File: `tx-pool/src/component/pool_map.rs`)

## Summary
In `PoolMap::add_entry`, `updated_stat_for_add_tx` captures a snapshot of `total_tx_size` and `total_tx_cycles` before `check_and_record_ancestors` runs. When the ancestor-count eviction branch fires inside `check_and_record_ancestors`, `remove_entry_and_descendants` → `remove_entry` → `update_stat_for_remove_tx` directly subtracts evicted entries from `self.total_tx_size`/`self.total_tx_cycles` in-place. Lines 218–219 then unconditionally overwrite those fields with the stale pre-eviction snapshot, permanently inflating both counters. The inflated `total_tx_size` causes `limit_size` to loop and evict valid, fee-paying transactions that would otherwise remain in the pool.

## Finding Description
**Root cause — `add_entry` lines 200–221 (`tx-pool/src/component/pool_map.rs`):**

```rust
// Step 1: snapshot BEFORE eviction (immutable &self call)
let (total_tx_size, total_tx_cycles) =
    self.updated_stat_for_add_tx(entry.size, entry.cycles)?;   // L210-211

// Step 2: may call remove_entry_and_descendants → remove_entry
//         → update_stat_for_remove_tx, which DIRECTLY MUTATES
//         self.total_tx_size / self.total_tx_cycles
evicts = self.check_and_record_ancestors(&mut entry)?;          // L213

// Step 3: OVERWRITES the in-place mutations from Step 2
self.total_tx_size = total_tx_size;                             // L218
self.total_tx_cycles = total_tx_cycles;                         // L219
```

`updated_stat_for_add_tx` (lines 711–729) is an immutable `&self` method returning `self.total_tx_size + entry.size` and `self.total_tx_cycles + entry.cycles` as a snapshot.

`check_and_record_ancestors` (lines 588–640) enters the eviction branch at line 603 when `ancestors_count.saturating_sub(cell_ref_parents.len()) <= self.max_ancestors_count` but `ancestors_count > self.max_ancestors_count`. It calls `remove_entry_and_descendants` → `remove_entry` (line 247), which calls `update_stat_for_remove_tx` — a `&mut self` method that directly subtracts the evicted entry's size and cycles from `self.total_tx_size` and `self.total_tx_cycles` (lines 738–740).

Lines 218–219 then write the stale snapshot back, erasing those subtractions. The result is that `total_tx_size` and `total_tx_cycles` reflect `(pre-eviction pool) + (new entry)` instead of `(post-eviction pool) + (new entry)`.

**Concrete trace (tx A size=100 in pool, tx B size=50 being added):**

| Step | `self.total_tx_size` |
|---|---|
| Before `add_entry` | 100 |
| After L210 (snapshot) | snapshot=150 |
| After L213 (A evicted via `update_stat_for_remove_tx`) | 0 |
| After L218 (stale overwrite) | **150** ← wrong |
| Correct (only B in pool) | 50 |

`limit_size` (`pool.rs` lines 292–329) loops `while self.pool_map.total_tx_size > self.config.max_tx_pool_size`, so the inflated value of 150 triggers eviction of tx B even though the pool is actually under the real limit.

## Impact Explanation
The inflated `total_tx_size` causes `limit_size` to continuously evict valid, fee-paying transactions from the mempool. An attacker who repeatedly submits crafted transactions can keep the pool in a permanent eviction loop, preventing honest users' transactions from being accepted or retained. The inflation compounds monotonically with each triggering submission. This constitutes **mempool denial-of-service with low cost per submission**, matching the High impact: *"Vulnerabilities or bad designs which could cause CKB network congestion with few costs."*

## Likelihood Explanation
The eviction branch at line 603 is reachable by any unprivileged user via `send_transaction` RPC or P2P relay. The attacker needs only to submit a transaction that references an existing pool transaction as a cell dep (`cell_ref_parents` non-empty) and has `ancestors_count > max_ancestors_count` while `ancestors_count - cell_ref_parents.len() <= max_ancestors_count`. This is a standard, supported transaction pattern requiring no privileged access, leaked keys, or majority hashpower. The inflation compounds monotonically with each triggering submission, making the attack cheap to sustain.

## Recommendation
Move the stat update to after `check_and_record_ancestors` returns, so evictions are already reflected before adding the new entry's contribution:

```rust
evicts = self.check_and_record_ancestors(&mut entry)?;
self.record_entry_edges(&entry)?;
self.insert_entry(&entry, status);
self.record_entry_descendants(&entry);
self.track_entry_statics(None, Some(status));
// Add only the new entry's contribution AFTER evictions are settled
self.total_tx_size = self.total_tx_size.saturating_add(entry.size);
self.total_tx_cycles = self.total_tx_cycles.saturating_add(entry.cycles);
```

Alternatively, remove `updated_stat_for_add_tx` entirely and introduce a mutable `update_stat_for_add_tx` that mirrors `update_stat_for_remove_tx`, called after `check_and_record_ancestors`.

## Proof of Concept
Deterministic reasoning (no external tooling required):

1. Pre-populate pool with tx A (size=100, cycles=1000). `total_tx_size = 100`.
2. Submit tx B (size=50, cycles=500) with a cell dep on A's output, with `ancestors_count = max_ancestors_count + 1` and `cell_ref_parents = {A}`, satisfying the branch condition at line 603.
3. `updated_stat_for_add_tx` at L210 snapshots `total_tx_size = 150`.
4. `check_and_record_ancestors` at L213 evicts A via `remove_entry_and_descendants` → `update_stat_for_remove_tx(100, 1000)` → `self.total_tx_size = 0`.
5. L218 sets `self.total_tx_size = 150` (stale snapshot).
6. Pool contains only tx B (size=50) but reports `total_tx_size = 150`.
7. `limit_size` at `pool.rs` L298 fires if `max_tx_pool_size < 150`, evicting tx B despite the pool being under the real limit.
8. Repeating step 2 with fresh transactions compounds the inflation monotonically.

### Citations

**File:** tx-pool/src/component/pool_map.rs (L210-211)
```rust
        let (total_tx_size, total_tx_cycles) =
            self.updated_stat_for_add_tx(entry.size, entry.cycles)?;
```

**File:** tx-pool/src/component/pool_map.rs (L218-219)
```rust
        self.total_tx_size = total_tx_size;
        self.total_tx_cycles = total_tx_cycles;
```

**File:** tx-pool/src/component/pool_map.rs (L616-621)
```rust
            while ancestors_count > self.max_ancestors_count {
                if let Some(next_id) = iter.next() {
                    let removed = self.remove_entry_and_descendants(next_id);
                    ancestors_count = ancestors_count.saturating_sub(1);
                    parents.remove(next_id);
                    evicted.extend(removed);
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

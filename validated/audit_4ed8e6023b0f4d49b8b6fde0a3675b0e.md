The code confirms the claim. Here is the verified trace:

**`add_entry`** [1](#0-0) 

- Line 210–211: `updated_stat_for_add_tx` takes `&self` (immutable borrow), computes `self.total_tx_size + entry.size`, and returns the result into a **local variable** without touching `self`. [2](#0-1) 
- Line 213: `check_and_record_ancestors` calls `remove_entry_and_descendants` in a loop when the eviction branch is entered. [3](#0-2) 
- `remove_entry_and_descendants` calls `remove_entry` for each removed tx. [4](#0-3) 
- `remove_entry` calls `update_stat_for_remove_tx`, which correctly decrements `self.total_tx_size`. [5](#0-4) 
- Lines 218–219 then unconditionally overwrite `self.total_tx_size` / `self.total_tx_cycles` with the **pre-eviction local snapshot**, erasing every decrement. [6](#0-5) 

All code references, the root cause, the exploit path, and the impact are confirmed by the actual source. The claim is valid.

---

Audit Report

## Title
`total_tx_size` / `total_tx_cycles` Inflated by Stale Snapshot Overwrite After Ancestor-Eviction in `add_entry` — (File: `tx-pool/src/component/pool_map.rs`)

## Summary

In `PoolMap::add_entry`, the new pool totals are snapshotted into local variables before `check_and_record_ancestors` runs. When that function evicts transactions via `remove_entry_and_descendants` → `remove_entry` → `update_stat_for_remove_tx`, it correctly decrements `self.total_tx_size` / `self.total_tx_cycles`. However, lines 218–219 then unconditionally overwrite those fields with the pre-eviction local snapshot, silently erasing every decrement. The result is a permanently inflated `total_tx_size` that causes `limit_size` to evict legitimate transactions and reject new submissions with `Reject::Full`, enabling a low-cost mempool DoS.

## Finding Description

The exact sequence in `add_entry` (lines 200–221 of `pool_map.rs`):

```rust
// Lines 210-211: snapshot self.total_tx_size + new_size into LOCAL variable
let (total_tx_size, total_tx_cycles) =
    self.updated_stat_for_add_tx(entry.size, entry.cycles)?;

// Line 213: may call remove_entry_and_descendants → remove_entry
//           → update_stat_for_remove_tx, which DECREMENTS self.total_tx_size
evicts = self.check_and_record_ancestors(&mut entry)?;

// Lines 218-219: OVERWRITE self fields with the stale pre-eviction snapshot
self.total_tx_size  = total_tx_size;
self.total_tx_cycles = total_tx_cycles;
```

`updated_stat_for_add_tx` (lines 711–729) takes `&self` (immutable borrow) and returns computed values without touching `self`. It captures `self.total_tx_size + new_size` at the moment of the call.

`check_and_record_ancestors` (lines 588–640) enters the eviction branch when `ancestors_count > max_ancestors_count` but `ancestors_count - cell_ref_parents.len() <= max_ancestors_count`, calling `remove_entry_and_descendants` in a loop. Each removal calls `remove_entry` (line 263), which calls `update_stat_for_remove_tx` (line 247), which correctly writes the decremented value back to `self.total_tx_size`. The final assignment at lines 218–219 then restores the stale snapshot, discarding all those decrements.

`remove_entry_and_descendants` (lines 252–265) calls `remove_entry` for each removed entry, and `remove_entry` (lines 235–250) calls `update_stat_for_remove_tx` at line 247.

**Net accounting error per eviction cycle:**
```
correct:  self.total_tx_size = S - E + new_size
actual:   self.total_tx_size = S + new_size        (E leaked in)
```

where `S` is the pre-call total and `E` is the sum of evicted transaction sizes. The error accumulates monotonically across repeated triggering.

Existing guards do not prevent this: the overflow check in `updated_stat_for_add_tx` only guards against integer overflow on addition; it does not prevent the stale-write problem. The underflow guard in `update_stat_for_remove_tx` correctly decrements `self.total_tx_size`, but that correct value is immediately overwritten.

## Impact Explanation

`limit_size` in `pool.rs` drives all size-based eviction directly from `pool_map.total_tx_size`. With an inflated counter, the pool evicts legitimate higher-fee transactions that would not otherwise be removed, and subsequent submissions are rejected with `Reject::Full` even though actual pool occupancy is below `max_tx_pool_size`. Repeated triggering accumulates inflation monotonically until the pool is effectively frozen for new submissions. This matches **High (10001–15000 points): Vulnerabilities or bad designs which could cause CKB network congestion with few costs.** The attacker pays only normal transaction fees and requires no privileged access.

## Likelihood Explanation

The eviction branch in `check_and_record_ancestors` requires a transaction where `ancestors_count > max_ancestors_count` (e.g., 26 ancestors with a limit of 25) and at least one ancestor is a `cell_ref_parent` (an existing pool transaction that references a live cell as a cell-dep, which the new transaction consumes as an input). This is a crafted but realistic transaction graph reachable by any unprivileged RPC or P2P sender. No key material, majority hashpower, or special privileges are required. The condition can be re-triggered repeatedly with fresh transaction graphs, accumulating inflation each time.

## Recommendation

Move the total computation to after all evictions complete, so it reads the already-decremented `self` fields:

```rust
evicts = self.check_and_record_ancestors(&mut entry)?;
self.record_entry_edges(&entry)?;
self.insert_entry(&entry, status);
self.record_entry_descendants(&entry);
self.track_entry_statics(None, Some(status));
// Now compute totals from the post-eviction self fields:
let (total_tx_size, total_tx_cycles) =
    self.updated_stat_for_add_tx(entry.size, entry.cycles)?;
self.total_tx_size  = total_tx_size;
self.total_tx_cycles = total_tx_cycles;
```

Alternatively, remove the local-variable pattern entirely and apply the increment directly to `self.total_tx_size` / `self.total_tx_cycles` after `check_and_record_ancestors` returns, using `checked_add` for overflow protection.

## Proof of Concept

**Setup:**
- `max_ancestors_count = 25`
- Pool contains transactions T1…T25 forming a chain; T25 also references live cell `O_base` as a cell-dep.
- Record initial `total_tx_size = S`, sum of T1…T25 sizes.

**Attack step (repeat to accumulate):**
1. Submit `T_attack` with inputs = [`O_base`] (consuming it), cell-deps = [outputs of T1…T25].
   - `ancestors_count = 26 > 25` → eviction branch entered.
   - `cell_ref_parents = {T25}` (T25 uses `O_base` as cell-dep; T_attack consumes it).
   - `26 - 1 = 25 <= 25` → eviction loop runs.
   - `remove_entry_and_descendants(T25)` → `remove_entry` → `update_stat_for_remove_tx(T25.size, T25.cycles)` → `self.total_tx_size = S - T25.size`.
   - T_attack is inserted.
   - Line 218 restores: `self.total_tx_size = S + T_attack.size` (T25.size is leaked back in).

2. After one cycle, `total_tx_size` is inflated by `T25.size` relative to actual pool contents.

3. Repeat with fresh transaction graphs. Each iteration adds another `E` bytes of phantom size.

4. Once `total_tx_size` exceeds `max_tx_pool_size`, `limit_size` evicts all pending transactions and rejects every new submission with `Reject::Full`, freezing the mempool.

**Verification:** A unit test can assert that after calling `add_entry` with a crafted entry that triggers the eviction branch, `pool_map.total_tx_size` equals the sum of sizes of all entries actually present in `pool_map.entries`, using the existing `recompute_total_stat` method as the ground truth.

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

**File:** tx-pool/src/component/pool_map.rs (L252-265)
```rust
    pub(crate) fn remove_entry_and_descendants(&mut self, id: &ProposalShortId) -> Vec<TxEntry> {
        let mut removed_ids = vec![id.to_owned()];
        removed_ids.extend(self.calc_descendants(id));

        // update links state for remove, so that we won't update_descendants_index_key in remove_entry
        for id in &removed_ids {
            self.remove_entry_links(id);
        }

        removed_ids
            .iter()
            .filter_map(|id| self.remove_entry(id))
            .collect()
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

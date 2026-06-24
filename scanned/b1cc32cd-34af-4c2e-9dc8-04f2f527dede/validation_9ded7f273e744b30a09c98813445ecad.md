The code confirms the claim exactly. Let me verify the call chain through `remove_entry_and_descendants`.

Audit Report

## Title
`total_tx_size`/`total_tx_cycles` Inflated After Ancestor-Eviction in `add_entry` — (File: tx-pool/src/component/pool_map.rs)

## Summary
In `PoolMap::add_entry`, `updated_stat_for_add_tx` captures a pre-eviction snapshot of `total_tx_size`/`total_tx_cycles` into local variables before `check_and_record_ancestors` runs. When `check_and_record_ancestors` evicts transactions via `remove_entry_and_descendants` → `remove_entry` → `update_stat_for_remove_tx`, those decrements are applied directly to `self.total_tx_size`/`self.total_tx_cycles`. The function then unconditionally overwrites those fields with the pre-eviction snapshot, permanently losing the eviction decrements. The result is that `total_tx_size` is inflated by the sum of sizes of all ancestor-evicted transactions, causing `limit_size` to over-evict valid transactions on every subsequent insertion.

## Finding Description

The exact sequence in `add_entry` (L200–221):

```rust
// L210-211: snapshot BEFORE eviction
let (total_tx_size, total_tx_cycles) =
    self.updated_stat_for_add_tx(entry.size, entry.cycles)?;
// = self.total_tx_size + entry.size  (local binding)

// L213: may evict N txs; each calls update_stat_for_remove_tx
//       which DECREMENTS self.total_tx_size in-place
evicts = self.check_and_record_ancestors(&mut entry)?;

// L218-219: OVERWRITES with pre-eviction snapshot — eviction decrements lost
self.total_tx_size = total_tx_size;
self.total_tx_cycles = total_tx_cycles;
``` [1](#0-0) 

`updated_stat_for_add_tx` reads `self.total_tx_size` at call time and returns a new value into a local binding without mutating `self`: [2](#0-1) 

`check_and_record_ancestors` calls `remove_entry_and_descendants` (L618), which calls `remove_entry` for each removed entry: [3](#0-2) 

`remove_entry` calls `update_stat_for_remove_tx` (L247), which directly mutates `self.total_tx_size` and `self.total_tx_cycles`: [4](#0-3) [5](#0-4) 

After the overwrite at L218–219, `self.total_tx_size` equals `(pre-eviction total) + entry.size`, ignoring all `−size(evicted_tx_i)` terms. The invariant `total_tx_size == Σ entry.size for all live entries` is broken. The inflation persists and compounds with each subsequent ancestor-eviction event.

## Impact Explanation

`limit_size` uses `self.pool_map.total_tx_size` as its sole loop condition: [6](#0-5) 

An inflated `total_tx_size` causes `limit_size` to evict additional valid transactions even though the actual pool is within its configured size limit. Each subsequent insertion that triggers the ancestor-eviction path compounds the inflation. Over time, the node's mempool will reject or evict legitimate transactions that should have been accepted. This fits **Low (501–2000 points): Any other important performance improvements for CKB** — it is a concrete correctness bug in a core mempool accounting invariant that degrades mempool utility for all users of the affected node.

## Likelihood Explanation

The trigger requires: (1) multiple in-pool transactions sharing a common cell dep (e.g., a popular lock script output used as a code dep); (2) a new transaction that consumes that cell dep as an input, making those transactions its `cell_ref_parents`; (3) the resulting ancestor count exceeds `max_ancestors_count` (default 25) but is reducible by evicting some `cell_ref_parents`. This is a realistic mainnet scenario. The entry path is the standard `send_transaction` RPC, reachable by any unprivileged submitter. No special privileges, keys, or majority hashpower are required. The inflation compounds on every repeated trigger.

## Recommendation

Move the stat update to after `check_and_record_ancestors` completes, so evictions are already reflected in `self.total_tx_size`, then add only the new entry's contribution:

```rust
evicts = self.check_and_record_ancestors(&mut entry)?;
self.record_entry_edges(&entry)?;
self.insert_entry(&entry, status);
self.record_entry_descendants(&entry);
self.track_entry_statics(None, Some(status));
self.total_tx_size = self.total_tx_size.saturating_add(entry.size);
self.total_tx_cycles = self.total_tx_cycles.saturating_add(entry.cycles);
```

This ensures the post-eviction `self.total_tx_size` is used as the base, so eviction decrements are not overwritten.

## Proof of Concept

**Setup:** Pool contains transactions A (size=100), B (size=100), C (size=100) all referencing cell dep `X`. `total_tx_size = 300`. `max_ancestors_count = 3`.

**Step 1:** Submit transaction D (size=50) that spends cell dep `X` as an input. D has 3 `cell_ref_parents` (A, B, C) → `ancestors_count = 4 > 3`. The eviction branch in `check_and_record_ancestors` is entered.

**Step 2:** A is evicted. `update_stat_for_remove_tx(100, ...)` → `self.total_tx_size = 200`.

**Step 3:** `add_entry` writes `self.total_tx_size = total_tx_size = 300 + 50 = 350`.

**Result:** Pool contains B, C, D with actual total size `250`, but `total_tx_size = 350` (inflated by 100).

**Consequence:** If `max_tx_pool_size = 300`, `limit_size` sees `350 > 300` and evicts B or C unnecessarily, even though the actual pool size (250) is within limits. Repeating the attack compounds the inflation.

A unit test can verify this by asserting `pool_map.total_tx_size == pool_map.entries.iter().map(|e| e.size).sum()` after calling `add_entry` in the ancestor-eviction scenario.

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

**File:** tx-pool/src/component/pool_map.rs (L252-264)
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

**File:** tx-pool/src/component/pool_map.rs (L733-741)
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

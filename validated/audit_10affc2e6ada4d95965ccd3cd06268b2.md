### Title
Wrong `ancestors_size` Used in Block-Assembly Loop Condition Causes Premature Transaction Rejection — (`tx-pool/src/component/tx_selector.rs`)

---

### Summary

In `TxSelector::txs_to_commit`, the per-iteration size/cycles guard uses `tx_entry.ancestors_size` and `tx_entry.ancestors_cycles` — the **total cumulative** weight of all ancestors including those already committed to the block — rather than the **incremental** weight of only the new ancestors not yet included. When a transaction's ancestors have already been added to the block but the transaction itself was not moved to `modified_entries` (due to the explicitly acknowledged inconsistency in `calc_descendants()`), the guard double-counts already-included ancestors, causing the transaction to be incorrectly rejected even though it fits within the block limits.

---

### Finding Description

In `TxSelector::txs_to_commit` (`tx-pool/src/component/tx_selector.rs`, lines 149–152):

```rust
let next_size = size.saturating_add(tx_entry.ancestors_size);
let next_cycles = cycles.saturating_add(tx_entry.ancestors_cycles);

if next_cycles > cycles_limit || next_size > size_limit {
```

- `size` is the running total of bytes of transactions **already committed** to the block template (accumulated at lines 213–214 via `entry.size`).
- `tx_entry.ancestors_size` is the **total** size of all ancestors of the transaction including itself, as stored in `TxEntry.ancestors_size`.

When a transaction's ancestors are already in `fetched_txs`, `size` already includes their bytes. Adding `ancestors_size` (which also includes those same ancestors) double-counts them, making `next_size` artificially large.

The `modified_entries` mechanism is designed to mitigate this: when ancestors are included, `update_modified_entries` calls `sub_ancestor_weight` on descendants to reduce their `ancestors_size`. However, the code itself explicitly notes:

```rust
// Note: since https://github.com/nervosnetwork/ckb/pull/3706
// calc_descendants() may not consistent
``` [1](#0-0) 

When `calc_descendants()` returns an incomplete set, some descendants are **not** moved to `modified_entries` and remain in the `sorted_proposed_iter()` with their original, inflated `ancestors_size`. These transactions are then evaluated with the overcounted `next_size`, causing them to be incorrectly rejected.

This is directly analogous to the TraitForge bug: `_tokenIds` (total across all generations) was used instead of `generationMintCounts[currentGeneration]` (per-generation count), causing the loop to stop prematurely. Here, `ancestors_size` (total across all ancestors, including already-included ones) is used instead of the effective incremental size, causing the block-assembly loop to reject transactions prematurely. [2](#0-1) 

The `TxEntry` struct confirms that `ancestors_size` includes the transaction itself and all its ancestors: [3](#0-2) 

The `sub_ancestor_weight` method that is supposed to correct this for `modified_entries` entries: [4](#0-3) 

The `update_modified_entries` call that relies on the potentially inconsistent `calc_descendants()`: [5](#0-4) 

---

### Impact Explanation

Transactions that would fit within the block's size and cycles limits are incorrectly rejected during block assembly. This results in:

- Blocks assembled with fewer transactions than the limits allow.
- Reduced miner revenue (fewer transaction fees collected per block).
- Reduced network throughput and longer user confirmation times.

A tx-pool submitter can trigger this by submitting a parent-child transaction chain where the parent is processed and included first (higher fee rate), and the child is not correctly moved to `modified_entries` due to the `calc_descendants()` inconsistency.

---

### Likelihood Explanation

The bug is conditional on `calc_descendants()` being inconsistent, which the codebase explicitly acknowledges can occur since PR #3706. Any transaction submitter can craft a parent-child transaction chain that exercises this code path. The likelihood is **medium**: it requires a specific ordering of transaction processing (parent included before child) and the `calc_descendants()` inconsistency condition, but both are reachable without any privileged access by any `tx-pool submitter` or `RPC caller` using `send_transaction`.

---

### Recommendation

For entries from `proposed_pool` (not in `modified_entries`), compute the effective incremental size/cycles by subtracting the sizes of already-fetched ancestors before the guard check:

```diff
  let short_id = tx_entry.proposal_short_id();
- let next_size = size.saturating_add(tx_entry.ancestors_size);
- let next_cycles = cycles.saturating_add(tx_entry.ancestors_cycles);
+ let already_fetched_size: usize = if !using_modified {
+     self.pool_map.calc_ancestors(&short_id)
+         .iter()
+         .filter(|id| self.fetched_txs.contains(*id))
+         .filter_map(|id| self.pool_map.get(id))
+         .map(|e| e.size)
+         .sum()
+ } else { 0 };
+ let already_fetched_cycles: Cycle = /* analogous */;
+ let next_size = size.saturating_add(
+     tx_entry.ancestors_size.saturating_sub(already_fetched_size));
+ let next_cycles = cycles.saturating_add(
+     tx_entry.ancestors_cycles.saturating_sub(already_fetched_cycles));
```

Alternatively, fix `calc_descendants()` to always return a consistent set so that all descendants are correctly moved to `modified_entries` with updated weights before the guard is evaluated.

---

### Proof of Concept

1. Submit transaction **A** (size=600, high fee rate) to the tx-pool.
2. Submit transaction **B** (size=500, lower fee rate, parent=A) to the tx-pool. B's stored `ancestors_size = 1100` (A.size + B.size).
3. Block `size_limit = 1100`.
4. `sorted_proposed_iter()` yields A first (higher fee rate). A is included. `size = 600`.
5. `update_modified_entries` calls `calc_descendants(A)`. Due to the acknowledged inconsistency, B is **not** returned and is **not** added to `modified_entries`.
6. B is encountered from `proposed_pool` with `ancestors_size = 1100`.
7. Guard check: `next_size = 600 + 1100 = 1700 > 1100` → B is **incorrectly rejected**.
8. Correct behavior: `size + B.size = 600 + 500 = 1100 ≤ 1100` → B **should be included**.

The block is assembled with only A instead of both A and B, leaving 500 bytes of capacity unused and the miner losing B's transaction fee.

### Citations

**File:** tx-pool/src/component/tx_selector.rs (L148-162)
```rust
            let short_id = tx_entry.proposal_short_id();
            let next_size = size.saturating_add(tx_entry.ancestors_size);
            let next_cycles = cycles.saturating_add(tx_entry.ancestors_cycles);

            if next_cycles > cycles_limit || next_size > size_limit {
                consecutive_failed += 1;
                if using_modified {
                    self.modified_entries.remove(&short_id);
                    self.failed_txs.insert(short_id.clone());
                }
                if consecutive_failed > MAX_CONSECUTIVE_FAILURES {
                    break;
                }
                continue;
            }
```

**File:** tx-pool/src/component/tx_selector.rs (L243-262)
```rust
    fn update_modified_entries(&mut self, already_added: &LinkedHashMap<ProposalShortId, TxEntry>) {
        for (id, entry) in already_added {
            let descendants = self.pool_map.calc_descendants(id);
            for desc_id in descendants
                .iter()
                .filter(|id| !already_added.contains_key(id) && self.pool_map.has_proposed(id))
            {
                // Note: since https://github.com/nervosnetwork/ckb/pull/3706
                // calc_descendants() may not consistent
                if let Some(mut desc) = self
                    .modified_entries
                    .remove(desc_id)
                    .or_else(|| self.pool_map.get(desc_id).cloned())
                {
                    desc.sub_ancestor_weight(entry);
                    self.modified_entries.insert_entry(desc);
                }
            }
        }
    }
```

**File:** tx-pool/src/component/entry.rs (L60-74)
```rust
        TxEntry {
            rtx,
            cycles,
            size,
            fee,
            timestamp,
            ancestors_size: size,
            ancestors_fee: fee,
            ancestors_cycles: cycles,
            descendants_fee: fee,
            descendants_size: size,
            descendants_cycles: cycles,
            descendants_count: 1,
            ancestors_count: 1,
        }
```

**File:** tx-pool/src/component/entry.rs (L157-166)
```rust
    pub fn sub_ancestor_weight(&mut self, entry: &TxEntry) {
        self.ancestors_count = self.ancestors_count.saturating_sub(1);
        self.ancestors_size = self.ancestors_size.saturating_sub(entry.size);
        self.ancestors_cycles = self.ancestors_cycles.saturating_sub(entry.cycles);
        self.ancestors_fee = Capacity::shannons(
            self.ancestors_fee
                .as_u64()
                .saturating_sub(entry.fee.as_u64()),
        );
    }
```

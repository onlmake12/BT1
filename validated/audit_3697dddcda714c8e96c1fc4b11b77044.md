Audit Report

## Title
Tx-Pool Size Limit Bypassed After Reorg via `readd_detached_tx` - (File: `tx-pool/src/process.rs`)

## Summary
During a chain reorganization, `update_tx_pool_for_reorg` calls `_update_tx_pool_for_reorg` (which enforces `limit_size` at its end) and then immediately calls `readd_detached_tx`, which re-inserts transactions from detached blocks with no subsequent size check. This allows `total_tx_size` to exceed `max_tx_pool_size` until the next ordinary transaction submission triggers `limit_size`, bypassing the node's configured memory budget for the tx-pool.

## Finding Description
In `update_tx_pool_for_reorg` (`tx-pool/src/process.rs`, lines 836–851), the reorg handler acquires the pool write-lock and calls two functions sequentially:

```rust
let mut tx_pool = self.tx_pool.write().await;
_update_tx_pool_for_reorg(...);          // ends with limit_size at line 1113
// notice: readd_detached_tx don't update cache
self.readd_detached_tx(&mut tx_pool, retain, fetched_cache).await;
// NO limit_size called after this
```

`_update_tx_pool_for_reorg` ends at line 1113 with:
```rust
let _ = tx_pool.limit_size(callbacks, None);
```

Immediately after, `readd_detached_tx` (lines 878–914) iterates over every transaction in detached blocks absent from the newly-attached chain (`retain = detached.difference(&attached)`). These were previously committed (not in the pool), so they represent a net addition to `total_tx_size`. The insertion path is:

```
readd_detached_tx
  → _submit_entry
    → add_pending / add_gap / add_proposed   (pool.rs:131-149)
      → pool_map.add_entry                   (pool_map.rs:200-221)
        → updated_stat_for_add_tx            (pool_map.rs:711-729)
```

`updated_stat_for_add_tx` (lines 711–729) only guards against arithmetic overflow via `checked_add`; it does **not** compare against `max_tx_pool_size`. The `max_tx_pool_size` guard lives exclusively in `limit_size` (`pool.rs:292–329`), which checks `self.pool_map.total_tx_size > self.config.max_tx_pool_size` in a loop and evicts entries. Since `limit_size` is never called after `readd_detached_tx`, the pool exits the reorg handler with `total_tx_size > max_tx_pool_size`.

All cited code is confirmed to match the claim exactly.

## Impact Explanation
This is a suboptimal implementation of the CKB tx-pool state storage mechanism. The `max_tx_pool_size` configuration is the node operator's primary memory-budget control for the tx-pool. After a reorg, the pool can exceed this limit by up to the aggregate serialized size of all transactions in the detached blocks (bounded by `max_block_bytes` per detached block, approximately 597 KB per block in CKB). The excess persists until the next ordinary transaction submission triggers `limit_size`. During this window, the node consumes more memory than configured and the eviction policy is not applied to the re-added transactions. This maps to **Medium (2001–10000 points): Suboptimal implementation of CKB state storage mechanism**.

## Likelihood Explanation
A single-block natural reorg (which occurs occasionally on any live network) is sufficient to trigger the condition if the detached block contains transactions. No attacker is required; normal network operation can trigger this. A miner with even a small fraction of hashpower could deliberately produce competing blocks to trigger repeated reorgs, but the excess is bounded per reorg event and self-corrects on the next transaction submission.

## Recommendation
Call `limit_size` after `readd_detached_tx` completes, inside the same lock scope:

```rust
{
    let mut tx_pool = self.tx_pool.write().await;
    _update_tx_pool_for_reorg(...);
    self.readd_detached_tx(&mut tx_pool, retain, fetched_cache).await;
    let _ = tx_pool.limit_size(&self.callbacks, None); // add this line
}
```

Alternatively, integrate the `max_tx_pool_size` check into `updated_stat_for_add_tx` so that every insertion path is uniformly guarded.

## Proof of Concept
1. Configure a node with `max_tx_pool_size = N` bytes and fill the pool to near capacity.
2. Produce a valid competing block containing transactions with aggregate serialized size S (S ≤ `max_block_bytes`, ~597 KB). These transactions must not be in the node's pool.
3. Trigger a 1-block reorg so the node's chain tip is replaced by the competing block. `update_tx_pool_for_reorg` is called.
4. `_update_tx_pool_for_reorg` runs; `limit_size` brings `total_tx_size` to ≤ N.
5. `readd_detached_tx` re-inserts the transactions from the now-detached block via `add_entry` → `updated_stat_for_add_tx`, adding S bytes to `total_tx_size` with no cap check.
6. Assert `tx_pool.pool_map.total_tx_size > N` immediately after the reorg handler exits — the pool exceeds the configured limit.
7. Confirm the excess persists until a new transaction is submitted, at which point `limit_size` is called and corrects the overrun.
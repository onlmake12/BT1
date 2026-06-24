Audit Report

## Title
Tokio Mutex Guard Held Across `.await` in `BlockTransactionsProcess::execute` — (`sync/src/relayer/block_transactions_process.rs`)

## Summary

`BlockTransactionsProcess::execute` acquires the `pending_compact_blocks` `tokio::sync::Mutex` and holds its guard across the `.await` point of `Relayer::reconstruct_block`, which internally awaits `tx_pool_controller.fetch_txs()`. Because `compact_block` is a `&mut` reference that borrows through the `OccupiedEntry` into the `MutexGuard`, Rust's borrow checker forces the guard to remain live for the entire `if let` block, including the inner await. Any connected peer can trigger this path with a single `BlockTransactions` message, serializing all other async tasks that need `pending_compact_blocks` for the full duration of the tx-pool round-trip.

## Finding Description

`pending_compact_blocks` is declared as `tokio::sync::Mutex<PendingCompactBlockMap>` in `SyncState`: [1](#0-0) 

The accessor returns a `tokio::sync::MutexGuard` via `.lock().await`: [2](#0-1) 

In `execute`, the guard is a temporary produced by the `.await` on line 68. Rust's temporary-lifetime-extension rules keep it alive for the entire `if let` block (lines 65–187). The `compact_block` variable on line 71 is a `&mut packed::CompactBlock` that borrows through `pending.get_mut()` into the guard: [3](#0-2) 

This reference is passed directly to `reconstruct_block`, forcing the borrow checker to keep the guard live across the `.await` at line 100: [4](#0-3) 

Inside `reconstruct_block`, `tx_pool.fetch_txs(short_ids_set).await` is an inter-task channel call that may not resolve immediately under any tx-pool load: [5](#0-4) 

While the guard is held, every other async task that calls `pending_compact_blocks().await` blocks, including the duplicate-check in `CompactBlockProcess::execute`: [6](#0-5) 

the post-reconstruction cleanup path: [7](#0-6) 

and `missing_or_collided_post_process`: [8](#0-7) 

## Impact Explanation

While the guard is held, the entire compact-block relay pipeline on the node is serialized: new compact blocks from any peer stall at the duplicate-check await, and post-reconstruction cleanup is also blocked. This delays block propagation for the affected node for the full duration of the tx-pool round-trip. The impact matches **Low (501–2000 points): Any other important performance improvements for CKB**, as it degrades block relay performance on a per-node basis without crashing the node or causing network-wide consensus deviation.

## Likelihood Explanation

Preconditions are minimal and arise during normal block relay: at least one compact block must be in the pending map (routine during any block announcement) and the tx pool must have any non-trivial load. Any connected, unprivileged peer can send a `BlockTransactions` message for a pending compact block. The rate limiter in `try_process` applies to `BlockTransactions` at 30 req/s per peer, but a single well-timed message is sufficient to open the contention window for the full tx-pool round-trip duration. [9](#0-8) 

## Recommendation

Clone the required data out of the map before releasing the lock, then re-acquire after reconstruction:

```rust
// 1. Acquire lock, clone needed data, release immediately.
let (compact_block_clone, expected_tx_indexes, expected_uncle_indexes) = {
    let mut guard = shared.state().pending_compact_blocks().await;
    let entry = guard.get_mut(&block_hash)?;
    let (cb, peers_map, _) = entry;
    let (tx_idx, uncle_idx) = peers_map.get_mut(&self.peer)?;
    (cb.clone(), tx_idx.clone(), uncle_idx.clone())
    // guard dropped here
};

// 2. Reconstruct without holding the lock.
let ret = self.relayer.reconstruct_block(
    &active_chain, &compact_block_clone, received_transactions,
    &expected_uncle_indexes, &received_uncles,
).await;

// 3. Re-acquire to update or remove the entry.
let mut guard = shared.state().pending_compact_blocks().await;
```

This eliminates the guard-across-await anti-pattern entirely and allows other tasks to access `pending_compact_blocks` concurrently during reconstruction.

## Proof of Concept

```rust
// Spawn task A: drives BlockTransactionsProcess::execute with a mock tx_pool
// that delays fetch_txs by 5 seconds.
//
// Spawn task B: calls shared.state().pending_compact_blocks().await
// and asserts it completes within 1 second.
//
// Without the fix, task B times out. With the fix, task B completes immediately.
tokio::select! {
    _ = task_b => { /* correct: lock not held */ }
    _ = tokio::time::sleep(Duration::from_secs(1)) => {
        panic!("pending_compact_blocks lock was held across await");
    }
}
```

### Citations

**File:** sync/src/types/mod.rs (L1332-1332)
```rust
    pending_compact_blocks: tokio::sync::Mutex<PendingCompactBlockMap>,
```

**File:** sync/src/types/mod.rs (L1372-1376)
```rust
    pub async fn pending_compact_blocks(
        &self,
    ) -> tokio::sync::MutexGuard<'_, PendingCompactBlockMap> {
        self.pending_compact_blocks.lock().await
    }
```

**File:** sync/src/relayer/block_transactions_process.rs (L65-71)
```rust
        if let Entry::Occupied(mut pending) = shared
            .state()
            .pending_compact_blocks()
            .await
            .entry(block_hash.clone())
        {
            let (compact_block, peers_map, _) = pending.get_mut();
```

**File:** sync/src/relayer/block_transactions_process.rs (L91-100)
```rust
                let ret = self
                    .relayer
                    .reconstruct_block(
                        &active_chain,
                        compact_block,
                        received_transactions,
                        expected_uncle_indexes,
                        &received_uncles,
                    )
                    .await;
```

**File:** sync/src/relayer/mod.rs (L113-123)
```rust
        let should_check_rate =
            !matches!(message, packed::RelayMessageUnionReader::CompactBlock(_));

        if should_check_rate
            && self
                .rate_limiter
                .check_key(&(peer, message.item_id()))
                .is_err()
        {
            return StatusCode::TooManyRequests.with_context(message.item_name());
        }
```

**File:** sync/src/relayer/mod.rs (L386-393)
```rust
        if !short_ids_set.is_empty() {
            let tx_pool = self.shared.shared().tx_pool_controller();
            let fetch_txs = tx_pool.fetch_txs(short_ids_set).await;
            if let Err(e) = fetch_txs {
                return ReconstructionResult::Error(StatusCode::TxPool.with_context(e));
            }
            txs_map.extend(fetch_txs.unwrap());
        }
```

**File:** sync/src/relayer/compact_block_process.rs (L106-107)
```rust
                let mut pending_compact_blocks = shared.state().pending_compact_blocks().await;
                pending_compact_blocks.remove(&block_hash);
```

**File:** sync/src/relayer/compact_block_process.rs (L284-291)
```rust
    let pending_compact_blocks = shared.state().pending_compact_blocks().await;
    if pending_compact_blocks
        .get(&block_hash)
        .map(|(_, peers_map, _)| peers_map.contains_key(&peer))
        .unwrap_or(false)
    {
        return StatusCode::CompactBlockIsAlreadyPending.with_context(block_hash);
    }
```

**File:** sync/src/relayer/compact_block_process.rs (L354-361)
```rust
    shared
        .state()
        .pending_compact_blocks()
        .await
        .entry(block_hash.clone())
        .or_insert_with(|| (compact_block, HashMap::default(), unix_time_as_millis()))
        .1
        .insert(peer, (missing_transactions.clone(), missing_uncles.clone()));
```

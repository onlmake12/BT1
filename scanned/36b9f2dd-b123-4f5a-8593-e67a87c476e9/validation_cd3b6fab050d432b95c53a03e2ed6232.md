Now I have all the information needed. Let me analyze the exact code path.

### Title
Tokio Mutex Guard Held Across `tx_pool.fetch_txs().await` in `BlockTransactionsProcess::execute` — (`sync/src/relayer/block_transactions_process.rs`)

### Summary

`BlockTransactionsProcess::execute` acquires the `pending_compact_blocks` Tokio async Mutex and holds its guard across the `.await` point of `Relayer::reconstruct_block`, which itself awaits `tx_pool_controller.fetch_txs()`. Any unprivileged peer that has a compact block in the pending map can trigger this path, serializing all other tasks that need `pending_compact_blocks` for the entire duration of the tx-pool round-trip.

### Finding Description

`pending_compact_blocks` is declared as a `tokio::sync::Mutex<PendingCompactBlockMap>` in `SyncState`: [1](#0-0) 

The accessor returns a `tokio::sync::MutexGuard`: [2](#0-1) 

In `BlockTransactionsProcess::execute`, the guard is obtained as a temporary in an `if let` scrutinee, and Rust's temporary-lifetime-extension rules keep it alive for the entire `if let` block: [3](#0-2) 

`compact_block` on line 71 is a `&mut packed::CompactBlock` that borrows through `pending.get_mut()` into the `MutexGuard`. Because this reference is passed directly to `reconstruct_block`, the borrow checker forces the guard to remain live across the entire call, including the inner await: [4](#0-3) 

`tx_pool.fetch_txs(short_ids_set).await` is an inter-task channel call to the tx-pool service. Under any tx-pool load the future may not resolve immediately, leaving the Tokio Mutex locked for an unbounded duration.

### Impact Explanation

While the guard is held, every other async task that calls `pending_compact_blocks().await` blocks:

- `contextual_check` inside `CompactBlockProcess::execute` (line 284 of `compact_block_process.rs`) — new compact blocks from any peer stall here. [5](#0-4) 
- The post-reconstruction cleanup path in `CompactBlockProcess::execute` (line 106). [6](#0-5) 
- `missing_or_collided_post_process` (line 354). [7](#0-6) 

A single slow `BlockTransactions` message therefore serializes the entire compact-block relay pipeline, delaying block propagation for the node.

### Likelihood Explanation

The preconditions are minimal: one compact block must be pending (normal during block relay) and the tx pool must be under any non-trivial load. Any connected peer can send a `BlockTransactions` message for a pending compact block without any privilege. The rate limiter in `try_process` explicitly skips `CompactBlock` messages but does apply to `BlockTransactions`; however, the rate limit is 30 req/s per peer, so a single well-timed message is sufficient to trigger the contention window. [8](#0-7) 

### Recommendation

Clone the data needed from the map before releasing the lock, then re-acquire after reconstruction:

```rust
// 1. Acquire lock, clone compact_block + indexes, release lock immediately.
let (compact_block_clone, expected_tx_indexes, expected_uncle_indexes) = {
    let guard = shared.state().pending_compact_blocks().await;
    let (cb, peers_map, _) = guard.get(&block_hash)?;
    let (tx_idx, uncle_idx) = peers_map.get(&self.peer)?;
    (cb.clone(), tx_idx.clone(), uncle_idx.clone())
    // guard dropped here
};

// 2. Reconstruct without holding the lock.
let ret = self.relayer.reconstruct_block(..., &compact_block_clone, ...).await;

// 3. Re-acquire to update or remove the entry.
let mut guard = shared.state().pending_compact_blocks().await;
```

This eliminates the guard-across-await anti-pattern entirely.

### Proof of Concept

```rust
// Spawn task A: holds pending_compact_blocks lock via BlockTransactionsProcess::execute
// with a mock tx_pool that delays fetch_txs by 5 seconds.
//
// Spawn task B: calls shared.state().pending_compact_blocks().await
// and asserts it does NOT complete within 1 second.
//
// Without the fix, task B times out. With the fix, task B completes immediately.
tokio::select! {
    _ = task_b => { /* should reach here quickly */ }
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

**File:** sync/src/relayer/block_transactions_process.rs (L65-100)
```rust
        if let Entry::Occupied(mut pending) = shared
            .state()
            .pending_compact_blocks()
            .await
            .entry(block_hash.clone())
        {
            let (compact_block, peers_map, _) = pending.get_mut();
            if let Entry::Occupied(mut value) = peers_map.entry(self.peer) {
                let (expected_transaction_indexes, expected_uncle_indexes) = value.get_mut();
                ckb_logger::info!(
                    "relayer receive BLOCKTXN of {}, peer: {}",
                    block_hash,
                    self.peer
                );

                attempt!(BlockTransactionsVerifier::verify(
                    compact_block,
                    expected_transaction_indexes,
                    &received_transactions,
                ));
                attempt!(BlockUnclesVerifier::verify(
                    compact_block,
                    expected_uncle_indexes,
                    &received_uncles,
                ));

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

**File:** sync/src/relayer/compact_block_process.rs (L106-117)
```rust
                let mut pending_compact_blocks = shared.state().pending_compact_blocks().await;
                pending_compact_blocks.remove(&block_hash);
                // remove all pending request below this block epoch
                //
                // use epoch as the judgment condition because we accept
                // all block in current epoch as uncle block
                pending_compact_blocks.retain(|_, (v, _, _)| {
                    Into::<EpochNumberWithFraction>::into(v.header().as_reader().raw().epoch())
                        .number()
                        >= block.epoch().number()
                });
                shrink_to_fit!(pending_compact_blocks, 20);
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

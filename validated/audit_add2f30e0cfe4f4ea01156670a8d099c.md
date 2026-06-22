Based on my thorough code analysis, here is my determination:

---

### Title
ChainService Thread Stall via Blocking `send()` During Orphan Resolution — (`chain/src/orphan_broker.rs`)

### Summary

When a parent block arrives and resolves N orphaned children, `accept_descendants` calls `send_unverified_block` for each child in a tight loop. Each call uses a **blocking** `crossbeam_channel::Sender::send()` on the `preload_unverified_tx` channel. If that channel is full, the ChainService thread stalls for the entire duration of the drain, blocking all subsequent block processing.

### Finding Description

**Channel is bounded and uses blocking send:**

The `preload_unverified_tx` channel is created with capacity `BLOCK_DOWNLOAD_WINDOW * 10 = 8192 * 10 = 81,920`: [1](#0-0) 

`ckb_channel` is a direct re-export of `crossbeam_channel`: [2](#0-1) 

`crossbeam_channel::Sender::send()` **blocks** until space is available. The call site in `send_unverified_block` uses exactly this blocking form: [3](#0-2) 

**The loop over N descendants is synchronous and on the ChainService thread:**

`accept_descendants` iterates all N orphans and calls `process_descendant` → `send_unverified_block` for each, with no non-blocking fallback: [4](#0-3) 

This is called from `process_lonely_block`, which is called from `asynchronous_process_block` on the ChainService thread: [5](#0-4) 

**Orphan pool has no hard size limit:**

`OrphanBlockPool::with_capacity(ORPHAN_BLOCK_SIZE)` uses `HashMap::with_capacity` as a pre-allocation hint only — `insert()` has no eviction or cap enforcement: [6](#0-5) [7](#0-6) 

**The channel can fill up:**

The PreloadUnverified thread drains `preload_unverified_tx` but then blocks on `unverified_block_tx.send()` (capacity 128) if the ConsumeUnverified thread is slow: [8](#0-7) 

When ConsumeUnverified is busy (e.g., verifying a heavy block in CKB-VM), the back-pressure propagates: `unverified_block_tx` fills → PreloadUnverified stalls → `preload_unverified_tx` fills.

**ChainService thread cannot process new blocks while blocked:**

The `start_process_block` loop cannot select on `process_block_rx` while the thread is blocked inside `send()`: [9](#0-8) 

### Impact Explanation

While the ChainService thread is blocked on `preload_unverified_tx.send()`, the `process_block_rx` channel (capacity 24) stops being drained. All peers' block submissions queue up and eventually stall. Block ingestion halts for all peers for the duration of the stall. This matches **Low (501–2000): ChainService stall degrades block ingestion for all peers**.

### Likelihood Explanation

An unprivileged peer can:
1. Send N structurally valid blocks (no PoW required — `non_contextual_verify` only checks structure) all referencing the same unknown parent hash, filling the orphan pool.
2. Ensure the `preload_unverified_tx` channel is near-full (by sending many valid blocks during a period when ConsumeUnverified is slow, e.g., during a complex script execution).
3. Deliver the parent block, triggering `accept_descendants(N)` with N blocking `send()` calls.

The `clean_expired_orphans` timer fires every 60 seconds but only evicts blocks from old epochs, not recently inserted ones.

### Recommendation

Replace the blocking `send()` in `send_unverified_block` with a non-blocking `try_send()` or `send_timeout()`. On failure (channel full), either drop the block with a log/metric, or use a separate queue/thread to drain orphan descendants asynchronously, decoupling orphan resolution from the ChainService event loop.

### Proof of Concept

1. Set `BLOCK_DOWNLOAD_WINDOW` to a small value or reduce channel capacity to 1 in a test build.
2. Insert 100 structurally valid orphan blocks under one fake parent hash via `asynchronous_process_lonely_block`.
3. Stall the PreloadUnverified thread (e.g., block `unverified_block_tx`).
4. Deliver the parent block.
5. Measure time the ChainService thread is blocked — it will be blocked for the entire duration it takes to drain 100 slots from the full channel.

### Citations

**File:** chain/src/init.rs (L22-22)
```rust
const ORPHAN_BLOCK_SIZE: usize = BLOCK_DOWNLOAD_WINDOW as usize;
```

**File:** chain/src/init.rs (L49-50)
```rust
    let (preload_unverified_tx, preload_unverified_rx) =
        channel::bounded::<LonelyBlockHash>(BLOCK_DOWNLOAD_WINDOW as usize * 10);
```

**File:** util/channel/src/lib.rs (L2-5)
```rust
pub use crossbeam_channel::{
    Receiver, RecvError, RecvTimeoutError, Select, SendError, Sender, TrySendError, after, bounded,
    select, tick, unbounded,
};
```

**File:** chain/src/orphan_broker.rs (L168-179)
```rust
        match self.preload_unverified_tx.send(lonely_block) {
            Ok(_) => {
                debug!(
                    "process desendant block success {}-{}",
                    block_number, block_hash
                );
            }
            Err(_) => {
                info!("send unverified_block_tx failed, the receiver has been closed");
                return;
            }
        };
```

**File:** chain/src/orphan_broker.rs (L206-210)
```rust
    fn accept_descendants(&self, descendants: Vec<LonelyBlockHash>) {
        for descendant_block in descendants {
            self.process_descendant(descendant_block);
        }
    }
```

**File:** chain/src/chain_service.rs (L43-69)
```rust
        loop {
            select! {
                recv(self.process_block_rx) -> msg => match msg {
                    Ok(Request { responder, arguments: lonely_block }) => {
                        // asynchronous_process_block doesn't interact with tx-pool,
                        // no need to pause tx-pool's chunk_process here.
                        let _trace_now = minstant::Instant::now();
                        self.asynchronous_process_block(lonely_block);
                        if let Some(handle) = ckb_metrics::handle(){
                            handle.ckb_chain_async_process_block_duration.observe(_trace_now.elapsed().as_secs_f64())
                        }
                        let _ = responder.send(());
                    },
                    _ => {
                        error!("process_block_receiver closed");
                        break;
                    },
                },
                recv(clean_expired_orphan_timer) -> _ => {
                    self.orphan_broker.clean_expired_orphans();
                },
                recv(signal_receiver) -> _ => {
                    info!("ChainService received exit signal, exit now");
                    break;
                }
            }
        }
```

**File:** chain/src/chain_service.rs (L143-143)
```rust
        self.orphan_broker.process_lonely_block(lonely_block.into());
```

**File:** chain/src/utils/orphan_block_pool.rs (L36-53)
```rust
    fn insert(&mut self, lonely_block: LonelyBlockHash) {
        let hash = lonely_block.hash();
        let parent_hash = lonely_block.parent_hash();
        self.blocks
            .entry(parent_hash.clone())
            .or_default()
            .insert(hash.clone(), lonely_block);
        // Out-of-order insertion needs to be deduplicated
        self.leaders.remove(&hash);
        // It is a possible optimization to make the judgment in advance,
        // because the parent of the block must not be equal to its own hash,
        // so we can judge first, which may reduce one arc clone
        if !self.parents.contains_key(&parent_hash) {
            // Block referenced by `parent_hash` is not in the pool,
            // and it has at least one child, the new inserted block, so add it to leaders.
            self.leaders.insert(parent_hash.clone());
        }
        self.parents.insert(hash, parent_hash);
```

**File:** chain/src/preload_unverified_blocks_channel.rs (L64-70)
```rust
        if self.unverified_block_tx.send(unverified_block).is_err() {
            info!(
                "send unverified_block to unverified_block_tx failed, the receiver has been closed"
            );
        } else {
            debug!("preload unverified block {}-{}", block_number, block_hash,);
        }
```

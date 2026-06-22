### Title
Unprotected `expect()` Panics in `PreloadUnverifiedBlocksChannel` Permanently Block the Block Verification Pipeline — (`chain/src/preload_unverified_blocks_channel.rs`)

---

### Summary

CKB's asynchronous block-processing pipeline uses a dedicated `preload_unverified_block` thread (`PreloadUnverifiedBlocksChannel`) that sits between the `ChainService` (non-contextual verification) and the `ConsumeUnverifiedBlocks` thread (contextual verification). This thread loads full block data from the store using two bare `.expect()` calls with no `catch_unwind` protection. If either call panics — which can be triggered by a race condition where a parent block is deleted from the store after failing contextual verification while its descendant is still queued for preloading — the thread dies permanently, dropping the `unverified_block_tx` sender. The `ConsumeUnverifiedBlocks` thread then exits on the resulting channel error. The entire block verification pipeline is permanently halted with no recovery path, analogous to the LayerZero blocking-receiver pattern.

---

### Finding Description

CKB's block processing pipeline (introduced in v0.118.0) is structured as a multi-stage pipeline across four threads:

1. **`ChainService`** — receives blocks via `process_block_rx`, performs non-contextual verification (including PoW), stores the block, and forwards a `LonelyBlockHash` to the `OrphanBroker`.
2. **`PreloadUnverifiedBlocksChannel`** — receives `LonelyBlockHash` items from `preload_unverified_rx`, loads the full block and parent header from the store, and sends `UnverifiedBlock` items to `unverified_block_tx`.
3. **`ConsumeUnverifiedBlocks`** — receives `UnverifiedBlock` items, performs full contextual verification, and on failure calls `delete_unverified_block` to remove the invalid block from the store. [1](#0-0) 

The `PreloadUnverifiedBlocksChannel::load_full_unverified_block_by_hash` function contains two bare `.expect()` calls: [2](#0-1) 

```rust
let block_view = self
    .shared
    .store()
    .get_block(&block_number_and_hash.hash())
    .expect("block stored");          // <-- panics if block deleted from store

let parent_header = {
    self.shared
        .store()
        .get_block_header(&parent_hash)
        .expect("parent header stored") // <-- panics if parent deleted from store
};
```

There is **no `catch_unwind`** wrapping in the `preload_unverified_block` thread's loop: [3](#0-2) 

By contrast, the `ConsumeUnverifiedBlocks` thread **does** use `catch_unwind` to survive panics: [4](#0-3) 

When `ConsumeUnverifiedBlocks` processes block N and it fails contextual verification, it calls `delete_unverified_block`, removing block N from the store: [5](#0-4) 

If block N+1 (a descendant of N) was already enqueued in `preload_unverified_rx` before N was deleted, the preload thread will subsequently attempt `get_block_header(&parent_hash)` where `parent_hash = hash(N)`. Since N has been deleted, this returns `None`, and `.expect("parent header stored")` **panics**. The thread dies, dropping `unverified_block_tx`. `ConsumeUnverifiedBlocks` then receives a channel error and exits: [6](#0-5) 

The pipeline is permanently dead. The `unverified_block_tx` channel is bounded at 128 items: [7](#0-6) 

and `preload_unverified_tx` is bounded at `BLOCK_DOWNLOAD_WINDOW * 10` items: [8](#0-7) 

meaning multiple descendant blocks can be queued ahead of the deletion event, making the race window wide.

---

### Impact Explanation

Once the `preload_unverified_block` thread panics and dies:

- The `unverified_block_tx` sender is dropped.
- `ConsumeUnverifiedBlocks` receives `Err` on `unverified_block_rx` and exits permanently.
- No further blocks can be contextually verified or committed to the chain.
- The node's chain tip freezes; it can no longer advance, effectively halting consensus participation.
- There is no automatic restart, no recovery path, and no operator-facing mechanism to resume the pipeline short of restarting the entire node process.

This is the direct CKB analog of the LayerZero "blocking receiver" pattern: an ordered processing queue is permanently stuck by a single bad entry, with no `forceResumeReceive`-equivalent.

---

### Likelihood Explanation

The attack requires an adversary to:

1. Produce block N that passes non-contextual verification (including valid PoW) but fails contextual verification (e.g., contains a transaction with an invalid script, capacity violation, or epoch rule violation).
2. Produce block N+1 as a valid descendant of N (also requiring valid PoW).
3. Relay both blocks to the victim node in rapid succession so that N+1 enters the preload queue before N is deleted by `ConsumeUnverifiedBlocks`.

The PoW requirement makes this expensive for ordinary peers but entirely feasible for a miner or mining pool with non-trivial hashrate. The race window is wide because the `preload_unverified_rx` channel holds up to `BLOCK_DOWNLOAD_WINDOW * 10 ≈ 81,920` items. A miner who can produce even one valid-PoW block with invalid transactions (e.g., a script that always fails) can chain a descendant and trigger the panic. The attack is permanent and requires only a one-time cost. [9](#0-8) 

---

### Recommendation

1. **Wrap the preload thread's loop body in `catch_unwind`**, mirroring the pattern already used in `ConsumeUnverifiedBlocks`. On panic, log the error, remove the block hash from `is_pending_verify`, and continue the loop rather than dying.

2. **Replace `.expect()` with graceful error handling** in `load_full_unverified_block_by_hash`. If `get_block` or `get_block_header` returns `None`, log a warning and skip the item (or invoke the verify callback with an error) rather than panicking.

3. **Consider a tombstone/skip mechanism**: when `ConsumeUnverifiedBlocks` deletes a block, it should mark all known descendants in the preload queue as invalid so the preload thread can skip them without attempting a store lookup.

---

### Proof of Concept

```
1. Attacker (miner) mines block N at the current chain tip.
   - Block N has valid PoW but contains a transaction with an always-failing lock script.
   - Block N passes ChainService non-contextual verification → stored → sent to preload queue.

2. Attacker immediately mines block N+1 (child of N) with valid PoW and valid transactions.
   - Block N+1 passes ChainService non-contextual verification → stored → sent to preload queue.

3. PreloadUnverifiedBlocksChannel processes N:
   - Loads block N and its parent header successfully.
   - Sends UnverifiedBlock(N) to unverified_block_tx.

4. ConsumeUnverifiedBlocks processes UnverifiedBlock(N):
   - Contextual verification fails (invalid script).
   - Calls delete_unverified_block(N) → block N removed from store.
   - Sets BLOCK_INVALID status for N.

5. PreloadUnverifiedBlocksChannel processes N+1 (still in preload queue):
   - get_block(&hash(N+1)) → Ok (N+1 still in store).
   - get_block_header(&hash(N)) → None (N was deleted).
   - .expect("parent header stored") → PANIC.

6. preload_unverified_block thread dies.
   - unverified_block_tx sender dropped.
   - ConsumeUnverifiedBlocks receives Err on unverified_block_rx → exits.

7. Node's block verification pipeline is permanently halted.
   - Chain tip frozen.
   - Node must be manually restarted.
``` [10](#0-9) [11](#0-10)

### Citations

**File:** chain/src/init.rs (L49-53)
```rust
    let (preload_unverified_tx, preload_unverified_rx) =
        channel::bounded::<LonelyBlockHash>(BLOCK_DOWNLOAD_WINDOW as usize * 10);

    let (unverified_queue_stop_tx, unverified_queue_stop_rx) = ckb_channel::bounded::<()>(1);
    let (unverified_block_tx, unverified_block_rx) = channel::bounded::<UnverifiedBlock>(128usize);
```

**File:** chain/src/preload_unverified_blocks_channel.rs (L33-51)
```rust
    pub(crate) fn start(&self) {
        loop {
            select! {
                recv(self.preload_unverified_rx) -> msg => match msg {
                    Ok(preload_unverified_block_task) =>{
                        self.preload_unverified_channel(preload_unverified_block_task);
                    },
                    Err(_err) =>{
                        info!("recv preload_task_rx failed");
                        break;
                    }
                },
                recv(self.stop_rx) -> _ => {
                    info!("preload_unverified_blocks thread received exit signal, exit now");
                    break;
                }
            }
        }
    }
```

**File:** chain/src/preload_unverified_blocks_channel.rs (L53-71)
```rust
    fn preload_unverified_channel(&self, task: LonelyBlockHash) {
        let block_number = task.block_number_and_hash.number();
        let block_hash = task.block_number_and_hash.hash();
        let unverified_block: UnverifiedBlock = self.load_full_unverified_block_by_hash(task);

        if let Some(metrics) = ckb_metrics::handle() {
            metrics
                .ckb_chain_unverified_block_ch_len
                .set(self.unverified_block_tx.len() as i64)
        };

        if self.unverified_block_tx.send(unverified_block).is_err() {
            info!(
                "send unverified_block to unverified_block_tx failed, the receiver has been closed"
            );
        } else {
            debug!("preload unverified block {}-{}", block_number, block_hash,);
        }
    }
```

**File:** chain/src/preload_unverified_blocks_channel.rs (L85-96)
```rust
        let block_view = self
            .shared
            .store()
            .get_block(&block_number_and_hash.hash())
            .expect("block stored");
        let block = Arc::new(block_view);
        let parent_header = {
            self.shared
                .store()
                .get_block_header(&parent_hash)
                .expect("parent header stored")
        };
```

**File:** chain/src/verify.rs (L71-126)
```rust
    pub(crate) fn start(mut self) {
        loop {
            let trace_begin_loop = minstant::Instant::now();
            select! {
                recv(self.unverified_block_rx) -> msg => match msg {
                    Ok(unverified_task) => {
                        // process this unverified block
                        if let Some(handle) = ckb_metrics::handle() {
                            handle.ckb_chain_consume_unverified_block_waiting_block_duration.observe(trace_begin_loop.elapsed().as_secs_f64())
                        }
                        let _ = self.tx_pool_controller.suspend_chunk_process();

                        let _trace_now = minstant::Instant::now();
                        let block_hash = unverified_task.block.hash();
                        let block_number = unverified_task.block.number();
                        if let Err(payload) = catch_unwind(AssertUnwindSafe(|| {
                            self.processor.consume_unverified_blocks(unverified_task);
                        })) {
                            error!(
                                "consume unverified block {}-{} panicked: {}",
                                block_number,
                                block_hash,
                                panic_payload_to_string(payload.as_ref())
                            );
                            self.processor.is_pending_verify.remove(&block_hash);
                        }
                        if let Some(handle) = ckb_metrics::handle() {
                            handle.ckb_chain_consume_unverified_block_duration.observe(_trace_now.elapsed().as_secs_f64())
                        }

                        let _ = self.tx_pool_controller.continue_chunk_process();
                    },
                    Err(err) => {
                        error!("unverified_block_rx err: {}", err);
                        return;
                    },
                },
                recv(self.truncate_block_rx) -> msg => match msg {
                    Ok(Request { responder, arguments: target_tip_hash }) => {
                        let _ = self.tx_pool_controller.suspend_chunk_process();
                        let _ = responder.send(self.processor.truncate(&target_tip_hash));
                        let _ = self.tx_pool_controller.continue_chunk_process();
                    },
                    Err(err) => {
                        info!("truncate_block_tx has been closed, err: {}", err);
                        return;
                    },
                },
                recv(self.stop_rx) -> _ => {
                    info!("verify_blocks thread received exit signal, exit now");
                    break;
                }

            }
        }
    }
```

**File:** chain/src/verify.rs (L153-190)
```rust
            Err(err) => {
                error!("verify block {} failed: {}", block_hash, err);

                let tip = self
                    .shared
                    .store()
                    .get_tip_header()
                    .expect("tip_header must exist");
                let tip_ext = self
                    .shared
                    .store()
                    .get_block_ext(&tip.hash())
                    .expect("tip header's ext must exist");

                self.shared.set_unverified_tip(ckb_shared::HeaderIndex::new(
                    tip.number(),
                    tip.hash(),
                    tip_ext.total_difficulty,
                ));

                self.delete_unverified_block(&block);

                if !is_internal_db_error(err) {
                    self.shared
                        .insert_block_status(block_hash.clone(), BlockStatus::BLOCK_INVALID);
                } else {
                    error!("internal db error, remove block status: {}", block_hash);
                    self.shared.remove_block_status(&block_hash);
                }

                error!(
                    "set_unverified tip to {}-{}, because verify {} failed: {}",
                    tip.number(),
                    tip.hash(),
                    block_hash,
                    err
                );
            }
```

**File:** util/constant/src/sync.rs (L54-54)
```rust
pub const BLOCK_DOWNLOAD_WINDOW: u64 = 1024 * 8; // 1024 * default_outbound_peers
```

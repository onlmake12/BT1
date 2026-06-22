All the critical code has been confirmed. Here is the analysis:

---

### Title
Blocking crossbeam `send` on bounded(24) channel inside async `SendBlock` handler stalls tokio worker threads — (`chain/src/chain_controller.rs`, `sync/src/synchronizer/mod.rs`)

### Summary

`ChainController::asynchronous_process_lonely_block` calls `Request::call_without_response`, which performs a **blocking** `crossbeam_channel::Sender::send()` on a `bounded(24)` channel. This call is reachable from the async `Synchronizer::try_process` handler for `SendBlock` messages — and unlike every other sync message type, `SendBlock` is **not** wrapped in `tokio::task::block_in_place`. When the channel is full, the tokio worker thread executing the async task blocks indefinitely, starving the runtime.

### Finding Description

**Channel creation** — `process_block_sender` is a crossbeam bounded channel with capacity 24: [1](#0-0) 

**Blocking send** — `Request::call_without_response` calls `sender.send()`, which is the crossbeam blocking variant (not `try_send`). The `let _ =` only discards the `Result`; the thread still blocks until space is available: [2](#0-1) 

**Call site** — `ChainController::asynchronous_process_lonely_block` invokes this directly: [3](#0-2) 

**Missing `block_in_place`** — In `Synchronizer::try_process` (an `async fn`), every other sync message type (`GetHeaders`, `SendHeaders`, `GetBlocks`) is wrapped in `tokio::task::block_in_place` to safely yield the worker thread. `SendBlock` is not: [4](#0-3) 

The full call chain for a `SendBlock` P2P message:

```
Synchronizer::try_process (async, tokio worker thread)
  └─ BlockProcess::execute()                          [no block_in_place]
       └─ Synchronizer::asynchronous_process_remote_block()
            └─ SyncShared::accept_remote_block()
                 └─ ChainController::asynchronous_process_remote_block()
                      └─ ChainController::asynchronous_process_lonely_block()
                           └─ Request::call_without_response()
                                └─ crossbeam Sender::send()  ← BLOCKS if channel full
``` [5](#0-4) [6](#0-5) 

### Impact Explanation

When the `process_block_sender` channel (capacity 24) is full, any tokio worker thread executing a `SendBlock` handler blocks at `sender.send()` until `ChainService` drains a slot. If an attacker sustains a flood of valid-header `SendBlock` messages, they can hold all tokio worker threads blocked simultaneously, stalling the entire P2P message processing loop — effectively partitioning the node from the network for the duration of the attack.

### Likelihood Explanation

During IBD, many block hashes already carry `HEADER_VALID` status (set by prior `SendHeaders` exchange), satisfying the `accept_remote_block` guard: [7](#0-6) 

An attacker controlling a handful of peers (or one peer with a pre-built chain segment) can send 24+ distinct `SendBlock` messages for `HEADER_VALID` blocks faster than `ChainService` drains them. `ChainService` processes blocks sequentially and performs a DB write (`insert_block`) per block: [8](#0-7) 

Under any I/O pressure, the channel stays full long enough to block multiple worker threads. The number of threads needed to stall the runtime equals the tokio worker thread count (typically CPU core count), achievable with 24 + N concurrent `SendBlock` messages.

### Recommendation

1. **Immediate**: Wrap the `SendBlock` arm in `tokio::task::block_in_place` to match the other handlers, preventing tokio worker thread starvation even if the send blocks.
2. **Better**: Replace `sender.send()` in `Request::call_without_response` with `sender.try_send()` and drop or log the block when the channel is full, making the function truly non-blocking.
3. **Structural**: Apply backpressure at the P2P ingress layer (rate-limit `SendBlock` per peer) so the channel cannot be saturated by a single attacker.

### Proof of Concept

1. Establish a header chain of 30 blocks so their hashes have `HEADER_VALID` status in the node's `block_status_map`.
2. Create a mock `ChainService` that never reads from `process_block_rx` (simulating slow DB).
3. Spawn 30 threads, each sending one `SendBlock` message via `BlockProcess::execute()`.
4. Assert that the 25th call does not return within a 1-second timeout — it will not, because `sender.send()` blocks with the channel at capacity 24.
5. Observe that no other sync messages (e.g., `GetHeaders`) can be processed on any tokio worker thread during the stall. [2](#0-1) [1](#0-0)

### Citations

**File:** chain/src/init.rs (L93-93)
```rust
    let (process_block_tx, process_block_rx) = channel::bounded(24);
```

**File:** util/channel/src/lib.rs (L44-50)
```rust
    pub fn call_without_response(sender: &Sender<Request<A, R>>, arguments: A) {
        let (responder, _response) = oneshot::channel();
        let _ = sender.send(Request {
            responder,
            arguments,
        });
    }
```

**File:** chain/src/chain_controller.rs (L52-63)
```rust
    pub fn asynchronous_process_remote_block(&self, remote_block: RemoteBlock) {
        let lonely_block = LonelyBlock {
            block: remote_block.block,
            verify_callback: Some(remote_block.verify_callback),
            switch: None,
        };
        self.asynchronous_process_lonely_block(lonely_block);
    }

    pub fn asynchronous_process_lonely_block(&self, lonely_block: LonelyBlock) {
        Request::call_without_response(&self.process_block_sender, lonely_block);
    }
```

**File:** sync/src/synchronizer/mod.rs (L396-418)
```rust
        match message {
            packed::SyncMessageUnionReader::GetHeaders(reader) => {
                tokio::task::block_in_place(|| {
                    GetHeadersProcess::new(reader, self, peer, &nc).execute()
                })
            }
            packed::SyncMessageUnionReader::SendHeaders(reader) => {
                tokio::task::block_in_place(|| {
                    HeadersProcess::new(reader, self, peer, &nc).execute()
                })
            }
            packed::SyncMessageUnionReader::GetBlocks(reader) => {
                tokio::task::block_in_place(|| {
                    GetBlocksProcess::new(reader, self, peer, &nc).execute()
                })
            }
            packed::SyncMessageUnionReader::SendBlock(reader) => {
                if reader.check_data() {
                    BlockProcess::new(reader, self, peer, nc).execute()
                } else {
                    StatusCode::ProtocolMessageIsMalformed.with_context("SendBlock is invalid")
                }
            }
```

**File:** sync/src/synchronizer/mod.rs (L470-486)
```rust
    pub fn asynchronous_process_remote_block(&self, remote_block: RemoteBlock) {
        let block_hash = remote_block.block.hash();
        let status = self.shared.active_chain().get_block_status(&block_hash);
        // NOTE: Filtering `BLOCK_STORED` but not `BLOCK_RECEIVED`, is for avoiding
        // stopping synchronization even when orphan_pool maintains dirty items by bugs.
        if status.contains(BlockStatus::BLOCK_STORED) {
            error!("Block {} already stored", block_hash);
        } else if status.contains(BlockStatus::HEADER_VALID) {
            self.shared.accept_remote_block(&self.chain, remote_block);
        } else {
            debug!(
                "Synchronizer process_new_block unexpected status {:?} {}",
                status, block_hash,
            );
            // TODO which error should we return?
        }
    }
```

**File:** sync/src/types/mod.rs (L1075-1087)
```rust
    pub(crate) fn accept_remote_block(&self, chain: &ChainController, remote_block: RemoteBlock) {
        {
            let entry = self
                .shared()
                .block_status_map()
                .entry(remote_block.block.header().hash());
            if let dashmap::mapref::entry::Entry::Vacant(entry) = entry {
                entry.insert(BlockStatus::BLOCK_RECEIVED);
            }
        }

        chain.asynchronous_process_remote_block(remote_block)
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

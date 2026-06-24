All cited code references check out against the actual repository. Every step of the call chain is confirmed:

- `try_process` is `async fn` [1](#0-0) 
- `GetHeaders`, `SendHeaders`, `GetBlocks` use `block_in_place`; `SendBlock` does not [2](#0-1) 
- `BlockProcess::execute` is a plain sync `fn` that calls `asynchronous_process_remote_block` directly [3](#0-2) 
- `asynchronous_process_remote_block` routes to `accept_remote_block` on `HEADER_VALID` [4](#0-3) 
- `accept_remote_block` calls `chain.asynchronous_process_remote_block` [5](#0-4) 
- `asynchronous_process_lonely_block` calls `Request::call_without_response` [6](#0-5) 
- `call_without_response` does a blocking `crossbeam_channel::Sender::send()` [7](#0-6) 
- Channel capacity is `bounded(24)` [8](#0-7) 
- `ChainService` drains one block per loop iteration sequentially [9](#0-8) 

---

Audit Report

## Title
Blocking crossbeam `send` on `bounded(24)` channel in async `SendBlock` handler stalls tokio worker threads — (`sync/src/synchronizer/mod.rs`, `chain/src/chain_controller.rs`)

## Summary

`Synchronizer::try_process` is an `async fn` running on a tokio worker thread. Its `SendBlock` arm calls `BlockProcess::execute()` — a plain synchronous function — without `tokio::task::block_in_place`, unlike every other message arm. The call chain terminates at `Request::call_without_response`, which performs a blocking `crossbeam_channel::Sender::send()` on a `bounded(24)` channel. When that channel is full, the tokio worker thread blocks indefinitely, starving the runtime and halting all P2P message processing.

## Finding Description

`try_process` is declared `async fn` at `sync/src/synchronizer/mod.rs:381`. The `GetHeaders`, `SendHeaders`, and `GetBlocks` arms are each wrapped in `tokio::task::block_in_place` (lines 398, 403, 408), which correctly signals to tokio that the thread will block and allows the runtime to spawn a replacement worker. The `SendBlock` arm at line 412 calls `BlockProcess::execute()` directly with no such wrapper.

`BlockProcess::execute` is a plain `fn` (not `async`). At line 75 it calls `self.synchronizer.asynchronous_process_remote_block(remote_block)` synchronously. When the block's status contains `HEADER_VALID`, `asynchronous_process_remote_block` (line 477) calls `self.shared.accept_remote_block`, which calls `chain.asynchronous_process_remote_block`, which calls `asynchronous_process_lonely_block`, which calls `Request::call_without_response(&self.process_block_sender, lonely_block)`.

`Request::call_without_response` in `util/channel/src/lib.rs:46` calls `sender.send(...)` where `sender` is a `crossbeam_channel::Sender` backed by a `bounded(24)` channel created at `chain/src/init.rs:93`. `crossbeam_channel::Sender::send` on a bounded channel blocks the calling thread until a slot is available. `ChainService` drains this channel one block at a time in a sequential loop, making it straightforward to saturate under any I/O pressure.

Because the blocking occurs inside an `async fn` on a tokio worker thread without `block_in_place`, tokio is unaware the thread is blocked and does not compensate. Once all worker threads are blocked at `sender.send()`, the entire tokio runtime stalls.

## Impact Explanation

When all tokio worker threads are blocked, no async task can be scheduled — including all other P2P message handlers, heartbeats, and sync logic. The node's network participation halts for the duration of the stall. This matches **High: Vulnerabilities which could easily crash a CKB node**.

## Likelihood Explanation

During IBD, many block hashes already carry `HEADER_VALID` status from prior `SendHeaders` exchanges, satisfying the guard in `asynchronous_process_remote_block`. An attacker controlling a single peer with a pre-built chain segment can send 24 + N distinct `SendBlock` messages for `HEADER_VALID` blocks faster than `ChainService` drains them. The number of concurrently blocked handlers needed to stall the runtime equals the tokio worker thread count (typically CPU core count), achievable with 24 + core-count messages. The attack is cheap, repeatable, and requires no special privilege beyond establishing a peer connection.

## Recommendation

1. **Immediate**: Wrap the `SendBlock` arm in `tokio::task::block_in_place` to match the other handlers, preventing tokio worker thread starvation even when the send blocks.
2. **Better**: Replace `sender.send()` in `Request::call_without_response` with `sender.try_send()` and drop or log the block when the channel is full, making the function truly non-blocking.
3. **Structural**: Apply per-peer rate-limiting on `SendBlock` at the P2P ingress layer so the channel cannot be saturated by a single attacker.

## Proof of Concept

1. Establish a header chain of 30 blocks so their hashes have `HEADER_VALID` status in the node's `block_status_map`.
2. Create a mock `ChainService` that never reads from `process_block_rx` (simulating a slow or stalled DB).
3. Spawn 30 concurrent tasks, each sending one `SendBlock` message through `BlockProcess::execute()` on a tokio worker thread.
4. Assert that the 25th call does not return within a 1-second timeout — it will not, because `sender.send()` blocks with the channel at capacity 24.
5. Observe that no other sync messages (e.g., `GetHeaders`) can be processed on any tokio worker thread during the stall, confirming full runtime starvation.

### Citations

**File:** sync/src/synchronizer/mod.rs (L381-386)
```rust
    async fn try_process(
        &self,
        nc: Arc<dyn CKBProtocolContext + Sync>,
        peer: PeerIndex,
        message: packed::SyncMessageUnionReader<'_>,
    ) -> Status {
```

**File:** sync/src/synchronizer/mod.rs (L397-418)
```rust
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

**File:** sync/src/synchronizer/mod.rs (L477-478)
```rust
        } else if status.contains(BlockStatus::HEADER_VALID) {
            self.shared.accept_remote_block(&self.chain, remote_block);
```

**File:** sync/src/synchronizer/block_process.rs (L34-76)
```rust
    pub fn execute(self) -> crate::Status {
        let block = Arc::new(self.message.block().to_entity().into_view());
        debug!(
            "BlockProcess received block {} {}",
            block.number(),
            block.hash(),
        );
        let shared = self.synchronizer.shared();

        if shared.new_block_received(&block) {
            let verify_callback = {
                let nc: Arc<dyn CKBProtocolContext + Sync> = Arc::clone(&self.nc);
                let peer_id: PeerIndex = self.peer;
                let block_hash: Byte32 = block.hash();
                Box::new(move |verify_result: Result<bool, ckb_error::Error>| {
                    match verify_result {
                        Ok(_) => {}
                        Err(err) => {
                            let is_internal_db_error = is_internal_db_error(&err);
                            if is_internal_db_error {
                                return;
                            }

                            // punish the malicious peer
                            post_sync_process(
                                nc.as_ref(),
                                peer_id,
                                "SendBlock",
                                StatusCode::BlockIsInvalid.with_context(format!(
                                    "block {} is invalid, reason: {}",
                                    block_hash, err
                                )),
                            );
                        }
                    };
                })
            };
            let remote_block = RemoteBlock {
                block,
                verify_callback,
            };
            self.synchronizer
                .asynchronous_process_remote_block(remote_block);
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

**File:** chain/src/chain_controller.rs (L61-63)
```rust
    pub fn asynchronous_process_lonely_block(&self, lonely_block: LonelyBlock) {
        Request::call_without_response(&self.process_block_sender, lonely_block);
    }
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

**File:** chain/src/init.rs (L93-93)
```rust
    let (process_block_tx, process_block_rx) = channel::bounded(24);
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

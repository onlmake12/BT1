Audit Report

## Title
Unbounded Async Task Accumulation via Unauthenticated `GetBlocks` Flooding — (`sync/src/synchronizer/get_blocks_process.rs`)

## Summary
`GetBlocksProcess::execute()` unconditionally spawns a Tokio task per valid block hash (up to 32 per message), each holding a full serialized block in memory. Unlike `Relayer`, the `Synchronizer` has no per-peer rate limiter for `GetBlocks` messages. An unprivileged connected peer can flood the node with `GetBlocks` messages while throttling its TCP receive window, causing tasks to accumulate without bound and exhausting node memory or starving the shared Tokio runtime.

## Finding Description

In `GetBlocksProcess::execute()`, for each valid block hash (up to `INIT_BLOCKS_IN_TRANSIT_PER_PEER = 32`), a Tokio task is unconditionally spawned: [1](#0-0) 

Each task captures a `message` containing the full serialized block (`block.data()`), which can be hundreds of KB to several MB per block.

`Synchronizer::try_process()` dispatches `GetBlocks` directly to `GetBlocksProcess::execute()` with no rate-limiting check: [2](#0-1) 

The `Synchronizer` struct has no `rate_limiter` field: [3](#0-2) 

By contrast, `Relayer` explicitly applies a `governor::RateLimiter` keyed by `(peer, message.item_id())` at 30 req/s before processing any message: [4](#0-3) [5](#0-4) 

The `async_send_message_to` path calls `async_p2p_control.send_message_to().await`, which sends into tentacle's internal bounded channel (configurable via `channel_size`): [6](#0-5) [7](#0-6) 

When the attacker throttles its TCP receive window, tentacle's send channel fills up, causing each spawned task's `.await` to yield — keeping the task alive and holding its block allocation. The Tokio runtime is an unbounded multi-thread scheduler with no cap on queued tasks: [8](#0-7) 

The per-message hash count is bounded at `MAX_HEADERS_LEN = 2000` by a malformed-message check, but the inner loop only iterates up to `INIT_BLOCKS_IN_TRANSIT_PER_PEER = 32`: [9](#0-8) 

There is no IBD check in `Synchronizer::received()` (unlike `Relayer::received()` which returns early during IBD), so `GetBlocks` is processed at all times.

## Impact Explanation

**High — Vulnerabilities which could easily crash a CKB node.**

Each spawned task holds a full serialized `SyncMessage` containing a block. At 32 tasks/message × sustained message rate × block size (e.g., 500 KB), memory grows rapidly. Once tentacle's send channel fills due to TCP receive window throttling, tasks remain live indefinitely. The shared `async_handle` is used by block processing, peer management, RPC, and relay; flooding it with thousands of pending I/O tasks degrades scheduling latency for all other async operations, eventually crashing or hanging the node. This is a single-node attack (not network-wide), placing it in the High severity tier.

## Likelihood Explanation

- The attacker only needs to be a connected peer (no privilege, no PoW, no key).
- Valid block hashes are publicly available from the chain tip.
- TCP receive window throttling is trivially achievable with standard OS socket options (`SO_RCVBUF`, `tc qdisc`).
- The default `max_peers = 125` means up to 125 simultaneous attackers could amplify the effect.
- The attack is locally testable and repeatable. [10](#0-9) 

## Recommendation

Add a per-peer rate limiter to `Synchronizer::try_process()` for `GetBlocks` messages, mirroring the existing `governor::RateLimiter` in `Relayer::try_process()`. Additionally, consider adding a global cap on the number of in-flight send tasks per peer, or switching to a bounded channel/semaphore pattern before spawning block-send tasks in `GetBlocksProcess::execute()`.

## Proof of Concept

1. Connect to a CKB node as a sync peer (no authentication required).
2. Collect 32 valid block hashes from the chain tip via the public RPC.
3. Set TCP receive buffer to near-zero (`setsockopt SO_RCVBUF = 256`) to throttle the node's outbound sends.
4. Send `GetBlocks` messages at maximum rate (e.g., 1,000+/s) in a tight loop over the established connection.
5. Monitor the node's RSS memory (`/proc/<pid>/status`) and Tokio task count (via metrics).
6. Observe unbounded memory growth and increasing RPC response latency on unrelated endpoints, eventually leading to OOM or runtime starvation.

### Citations

**File:** sync/src/synchronizer/get_blocks_process.rs (L36-45)
```rust
        if block_hashes.len() > MAX_HEADERS_LEN {
            return StatusCode::ProtocolMessageIsMalformed.with_context(format!(
                "BlockHashes count({}) > MAX_HEADERS_LEN({})",
                block_hashes.len(),
                MAX_HEADERS_LEN,
            ));
        }
        let active_chain = self.synchronizer.shared.active_chain();

        let iter = block_hashes.iter().take(INIT_BLOCKS_IN_TRANSIT_PER_PEER);
```

**File:** sync/src/synchronizer/get_blocks_process.rs (L75-83)
```rust
                let content = packed::SendBlock::new_builder().block(block.data()).build();
                let message = packed::SyncMessage::new_builder().set(content).build();

                let nc = Arc::clone(self.nc);
                self.synchronizer
                    .shared()
                    .shared()
                    .async_handle()
                    .spawn(async move { async_send_message_to(&nc, self.peer, &message).await });
```

**File:** sync/src/synchronizer/mod.rs (L357-362)
```rust
pub struct Synchronizer {
    pub(crate) chain: ChainController,
    /// Sync shared state
    pub shared: Arc<SyncShared>,
    fetch_channel: Option<channel::Sender<FetchCMD>>,
}
```

**File:** sync/src/synchronizer/mod.rs (L407-411)
```rust
            packed::SyncMessageUnionReader::GetBlocks(reader) => {
                tokio::task::block_in_place(|| {
                    GetBlocksProcess::new(reader, self, peer, &nc).execute()
                })
            }
```

**File:** sync/src/relayer/mod.rs (L81-82)
```rust
    rate_limiter: RateLimiter<(PeerIndex, u32)>,
}
```

**File:** sync/src/relayer/mod.rs (L116-123)
```rust
        if should_check_rate
            && self
                .rate_limiter
                .check_key(&(peer, message.item_id()))
                .is_err()
        {
            return StatusCode::TooManyRequests.with_context(message.item_name());
        }
```

**File:** network/src/protocols/mod.rs (L500-510)
```rust
    async fn async_send_message_to(&self, peer_index: PeerIndex, data: Bytes) -> Result<(), Error> {
        trace!(
            "[send message to]: {}, to={}, length={}",
            self.proto_id,
            peer_index,
            data.len()
        );
        self.async_p2p_control
            .send_message_to(peer_index, self.proto_id, data)
            .await?;
        Ok(())
```

**File:** util/app-config/src/configs/network.rs (L95-96)
```rust
    /// Tentacle inner channel_size.
    pub channel_size: Option<usize>,
```

**File:** util/runtime/src/native.rs (L85-112)
```rust
fn new_runtime(worker_num: Option<usize>) -> Runtime {
    Builder::new_multi_thread()
        .enable_all()
        .worker_threads(worker_num.unwrap_or_else(|| available_parallelism().unwrap().into()))
        .thread_name_fn(|| {
            static ATOMIC_ID: AtomicU32 = AtomicU32::new(0);
            let id = ATOMIC_ID
                .fetch_update(Ordering::SeqCst, Ordering::SeqCst, |n| {
                    // A long thread name will cut to 15 characters in debug tools.
                    // Such as "top", "htop", "gdb" and so on.
                    // It's a kernel limit.
                    //
                    // So if we want to see the whole name in debug tools,
                    // this number should have 6 digits at most,
                    // since the prefix uses 9 characters in below code.
                    //
                    // There is still an issue:
                    // When id wraps around, we couldn't know whether the old id
                    // is released or not.
                    // But we can ignore this, because it's almost impossible.
                    if n >= 999_999 { Some(0) } else { Some(n + 1) }
                })
                .expect("impossible since the above closure must return Some(number)");
            format!("GlobalRt-{id}")
        })
        .build()
        .expect("ckb runtime initialized")
}
```

**File:** resource/ckb.toml (L94-95)
```text
max_peers = 125
max_outbound_peers = 8
```

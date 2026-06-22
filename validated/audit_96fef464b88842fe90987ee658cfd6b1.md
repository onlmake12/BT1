Based on my analysis of the code, here is my determination:

---

### Title
Unbounded Async Task Accumulation via Unauthenticated GetBlocks Flooding — (`sync/src/synchronizer/get_blocks_process.rs`)

### Summary
`GetBlocksProcess::execute()` spawns up to `INIT_BLOCKS_IN_TRANSIT_PER_PEER` (32) Tokio tasks per received `GetBlocks` message, each holding a full serialized block in memory. The `Synchronizer` has **no rate limiter** for `GetBlocks` messages, unlike the `Relayer` which applies a `governor::RateLimiter` at 30 req/s per peer. An unprivileged peer can flood the node with `GetBlocks` messages while throttling its TCP receive window, causing tasks to accumulate without bound and exhausting memory or starving the shared Tokio runtime.

### Finding Description

In `GetBlocksProcess::execute()`, for each valid block hash in the message (up to `INIT_BLOCKS_IN_TRANSIT_PER_PEER = 32`), a new async task is unconditionally spawned:

```rust
self.synchronizer
    .shared()
    .shared()
    .async_handle()
    .spawn(async move { async_send_message_to(&nc, self.peer, &message).await });
``` [1](#0-0) 

Each task captures a `message` containing the full serialized block (`block.data()`), which can be hundreds of KB to several MB per block. [2](#0-1) 

The `Synchronizer::try_process()` dispatches `GetBlocks` directly to `GetBlocksProcess::execute()` with no rate-limiting check:

```rust
packed::SyncMessageUnionReader::GetBlocks(reader) => {
    tokio::task::block_in_place(|| {
        GetBlocksProcess::new(reader, self, peer, &nc).execute()
    })
}
``` [3](#0-2) 

The `Synchronizer` struct has no `rate_limiter` field at all: [4](#0-3) 

By contrast, the `Relayer` explicitly applies a `governor::RateLimiter` keyed by `(peer, message.item_id())` at 30 req/s before processing any message: [5](#0-4) 

The Tokio runtime used is an unbounded multi-thread scheduler — `Handle::spawn()` delegates directly to `tokio::Handle::spawn()` with no task queue depth limit: [6](#0-5) 

The runtime is created with `new_multi_thread()` and `available_parallelism()` worker threads, with no cap on queued tasks: [7](#0-6) 

The per-message hash count is bounded at `MAX_HEADERS_LEN = 2000` by a malformed-message check, but the inner loop only iterates up to `INIT_BLOCKS_IN_TRANSIT_PER_PEER = 32`: [8](#0-7) 

### Impact Explanation

- **Memory exhaustion**: Each spawned task holds a full serialized `SyncMessage` containing a block. At 32 tasks/message × N messages/second × block size (e.g., 500 KB), memory grows rapidly. At 1000 messages/second, this is ~16 GB/s of task-held block data accumulating in the Tokio heap.
- **Runtime starvation**: The shared `async_handle` is used by block processing, peer management, RPC, and relay. Flooding it with thousands of pending I/O tasks (each waiting on a throttled TCP send) degrades scheduling latency for all other async operations on the node.
- **No backpressure**: `async_send_message_to` is async and yields on a slow peer, but the task itself remains live and holds its memory allocation until the send completes or the connection drops.

### Likelihood Explanation

- The attacker only needs to be a connected peer (no privilege, no PoW, no key).
- Valid block hashes are publicly available from the chain.
- TCP receive window throttling is trivially achievable with standard OS socket options (`SO_RCVBUF`, `tc qdisc`, etc.).
- The attack is locally testable: connect a throttled peer, send GetBlocks at high rate, measure Tokio task count and RSS growth.

### Recommendation

Add a per-peer rate limiter to `Synchronizer::try_process()` for `GetBlocks` messages, mirroring the existing `governor::RateLimiter` in `Relayer::try_process()`. Additionally, consider adding a global cap on the number of in-flight send tasks per peer, or switching to a bounded channel/semaphore pattern before spawning block-send tasks.

### Proof of Concept

1. Connect to a CKB node as a sync peer.
2. Collect 32 valid block hashes from the chain tip.
3. Set TCP receive buffer to near-zero (`setsockopt SO_RCVBUF = 256`) to throttle the node's sends.
4. Send `GetBlocks` messages at maximum rate (e.g., 10,000/s) in a tight loop.
5. Monitor the node's RSS memory and Tokio task count (via metrics or `/proc/<pid>/status`).
6. Observe unbounded memory growth and increasing RPC response latency on unrelated endpoints.

### Citations

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

**File:** sync/src/relayer/mod.rs (L88-123)
```rust
    pub fn new(chain: ChainController, shared: Arc<SyncShared>) -> Self {
        // setup a rate limiter keyed by peer and message type that lets through 30 requests per second
        // current max rps is 10 (ASK_FOR_TXS_TOKEN / TX_PROPOSAL_TOKEN), 30 is a flexible hard cap with buffer
        let quota = governor::Quota::per_second(std::num::NonZeroU32::new(30).unwrap());
        let rate_limiter = RateLimiter::hashmap(quota);

        Relayer {
            chain,
            shared,
            rate_limiter,
        }
    }

    /// Get shared state
    pub fn shared(&self) -> &Arc<SyncShared> {
        &self.shared
    }

    async fn try_process(
        &mut self,
        nc: Arc<dyn CKBProtocolContext + Sync>,
        peer: PeerIndex,
        message: packed::RelayMessageUnionReader<'_>,
    ) -> Status {
        // CompactBlock will be verified by POW, it's OK to skip rate limit checking.
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

**File:** util/runtime/src/native.rs (L47-60)
```rust
    pub fn spawn<F>(&self, future: F) -> JoinHandle<F::Output>
    where
        F: Future + Send + 'static,
        F::Output: Send + 'static,
    {
        let tokio_task_guard = self.guard.clone();

        self.inner.spawn(async move {
            // move tokio_task_guard into the spawned future
            // so that it will be dropped when the future is finished
            let _guard = tokio_task_guard;
            future.await
        })
    }
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

**File:** util/constant/src/sync.rs (L8-16)
```rust
pub const MAX_HEADERS_LEN: usize = 2_000;

// The default number of download blocks that can be requested at one time
/* About Download Scheduler */

/// ckb2021 edition new limit
pub const INIT_BLOCKS_IN_TRANSIT_PER_PEER: usize = 32;
/// Maximum number of download blocks that can be requested at one time
pub const MAX_BLOCKS_IN_TRANSIT_PER_PEER: usize = 128;
```

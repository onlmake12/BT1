Based on my analysis of the code, here is my assessment:

---

### Title
Unbounded `async_handle().spawn()` Task Accumulation via Unauthenticated `GetHeaders` Flood — (`sync/src/synchronizer/get_headers_process.rs`)

### Summary

An unprivileged remote peer can flood a post-IBD CKB node with valid `GetHeaders` messages, causing `GetHeadersProcess::execute()` to call `async_handle().spawn()` without any bound, rate limit, or backpressure. Each spawned task holds a serialized `SendHeaders` message (up to 2,000 × ~192 bytes ≈ 384 KB). Tasks accumulate unboundedly in the Tokio runtime, leading to memory exhaustion and starvation of other async tasks (block processing, relay, RPC).

### Finding Description

In `GetHeadersProcess::execute()`, every `GetHeaders` message that passes two cheap checks — locator size ≤ `MAX_LOCATOR_SIZE` (101) and genesis hash as the last locator entry — causes an unconditional fire-and-forget spawn:

```rust
// sync/src/synchronizer/get_headers_process.rs, lines 89–93
self.synchronizer
    .shared()
    .shared()
    .async_handle()
    .spawn(async move { async_send_message_to(&nc, self.peer, &message).await });
``` [1](#0-0) 

`Handle::spawn()` is a thin wrapper over Tokio's unbounded `spawn`:

```rust
// util/runtime/src/native.rs, lines 47–60
pub fn spawn<F>(&self, future: F) -> JoinHandle<F::Output> { ... self.inner.spawn(...) }
``` [2](#0-1) 

There is **no rate limiter** in the `Synchronizer` for any sync message type. Contrast this with the `Relayer`, which explicitly installs a `governor`-based rate limiter capped at 30 req/s per peer per message type:

```rust
// sync/src/relayer/mod.rs, lines 91–92
let quota = governor::Quota::per_second(std::num::NonZeroU32::new(30).unwrap());
let rate_limiter = RateLimiter::hashmap(quota);
``` [3](#0-2) 

The `Synchronizer::try_process()` match arm for `GetHeaders` has no equivalent guard:

```rust
// sync/src/synchronizer/mod.rs, lines 397–401
packed::SyncMessageUnionReader::GetHeaders(reader) => {
    tokio::task::block_in_place(|| {
        GetHeadersProcess::new(reader, self, peer, &nc).execute()
    })
}
``` [4](#0-3) 

The `locate_latest_common_block` check that gates the spawn only requires the last locator hash to equal the genesis hash — trivially satisfiable by any peer:

```rust
// sync/src/types/mod.rs, lines 1866–1868
let locator_hash = locator.last().expect("empty checked");
if locator_hash != &self.sync_shared.consensus().genesis_hash() {
    return None;
}
``` [5](#0-4) 

Each spawned task carries a `SendHeaders` payload of up to `MAX_HEADERS_LEN` = 2,000 headers:

```rust
// util/constant/src/sync.rs, line 8
pub const MAX_HEADERS_LEN: usize = 2_000;
``` [6](#0-5) 

### Impact Explanation

- **Memory exhaustion**: Each pending task holds a `SendHeaders` message of up to ~384 KB. 10,000 queued tasks = ~3.84 GB heap pressure from message payloads alone, plus Tokio task metadata.
- **Task starvation**: The shared Tokio runtime is used for block processing, relay, RPC, and all other async work. An unbounded task queue starves these subsystems of executor time.
- **Node degradation or crash**: OOM kill or livelock of critical subsystems (block acceptance, relay propagation, RPC responsiveness).

### Likelihood Explanation

The preconditions are all reachable in normal mainnet operation:
1. Establish a standard P2P connection (no privilege required).
2. Wait for the target node to exit IBD (normal state for any synced node).
3. Send `GetHeaders` messages in a tight loop with a valid locator (genesis hash as last entry, any hashes before it). No PoW, no key, no special role required.

The attack is cheap for the attacker (small messages sent) and expensive for the victim (large `SendHeaders` messages allocated and queued per request).

### Recommendation

Apply a per-peer rate limit to `GetHeaders` processing in the `Synchronizer`, mirroring the existing `RateLimiter` in the `Relayer`. A limit of 1–5 `GetHeaders` per second per peer is sufficient for legitimate sync behavior. Additionally, consider bounding the total number of outstanding `SendHeaders` spawn tasks (e.g., via a `tokio::sync::Semaphore`) to enforce a hard cap on queued work regardless of peer count.

### Proof of Concept

1. Connect to a post-IBD CKB node via the Sync P2P protocol.
2. In a tight loop, send `SyncMessage::GetHeaders` with `block_locator_hashes = [<any_hash>, <genesis_hash>]` and `hash_stop = Byte32::zero()`.
3. Monitor Tokio task count (via metrics or `/proc/<pid>/status` VmRSS) and heap allocation.
4. Assert: after 10,000 messages, task queue depth is unbounded and heap grows proportionally to `N × MAX_HEADERS_LEN × header_size`.
5. Observe RPC latency and block relay degradation as the runtime is saturated.

### Citations

**File:** sync/src/synchronizer/get_headers_process.rs (L89-93)
```rust
            self.synchronizer
                .shared()
                .shared()
                .async_handle()
                .spawn(async move { async_send_message_to(&nc, self.peer, &message).await });
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

**File:** sync/src/relayer/mod.rs (L91-92)
```rust
        let quota = governor::Quota::per_second(std::num::NonZeroU32::new(30).unwrap());
        let rate_limiter = RateLimiter::hashmap(quota);
```

**File:** sync/src/synchronizer/mod.rs (L397-401)
```rust
            packed::SyncMessageUnionReader::GetHeaders(reader) => {
                tokio::task::block_in_place(|| {
                    GetHeadersProcess::new(reader, self, peer, &nc).execute()
                })
            }
```

**File:** sync/src/types/mod.rs (L1866-1868)
```rust
        let locator_hash = locator.last().expect("empty checked");
        if locator_hash != &self.sync_shared.consensus().genesis_hash() {
            return None;
```

**File:** util/constant/src/sync.rs (L8-8)
```rust
pub const MAX_HEADERS_LEN: usize = 2_000;
```

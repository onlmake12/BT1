### Title
Missing Per-Peer Rate Limiting on `GetBlockFilters` Enables Resource Exhaustion DoS — (`sync/src/filter/get_block_filters_process.rs`)

### Summary

The `BlockFilter` protocol handler has no per-peer rate limiting for `GetBlockFilters` messages. Any unprivileged remote peer can send these messages in a tight loop, each triggering up to 1,000 RocksDB reads and a ~1.8 MB response allocation, with no throttle. The analogous `Relayer` protocol explicitly implements a `RateLimiter<(PeerIndex, u32)>` at 30 req/s per peer — the `BlockFilter` handler has no such guard.

### Finding Description

`GetBlockFiltersProcess::execute()` performs up to `BATCH_SIZE=1000` sequential store reads and accumulates `block_hashes` and `filters` Vecs capped at 1.8 MB before serializing and sending the response: [1](#0-0) [2](#0-1) [3](#0-2) 

The `BlockFilter` struct that dispatches this handler carries no rate limiter field and performs no rate check before calling `execute()`: [4](#0-3) [5](#0-4) 

By contrast, the `Relayer` protocol explicitly defines and enforces a per-peer, per-message-type rate limit of 30 req/s: [6](#0-5) [7](#0-6) [8](#0-7) [9](#0-8) 

No equivalent guard exists anywhere in `sync/src/filter/`:



### Impact Explanation

Each `GetBlockFilters(start_number=0)` request causes:
- Up to 1,000 RocksDB reads (`get_block_hash` + `get_block_filter` per block)
- Up to ~1.8 MB heap allocation for `block_hashes` + `filters` Vecs
- Molecule serialization of the full response
- An async send via `async_send_message_to`

With no rate limit, a single peer can saturate the async task queue and disk I/O. Multiple peers amplify this linearly. The result is CPU/I/O exhaustion and memory pressure leading to node unresponsiveness or OOM.

### Likelihood Explanation

The attack requires only a valid P2P connection and the ability to send well-formed `GetBlockFilters` messages — no authentication, no PoW, no stake. The `GetBlockFilters` molecule struct is a single `Uint64` field, trivial to construct: [10](#0-9) 

The precondition (node has filters built for a long chain) is the normal production state of any full node with `block_filter` enabled.

### Recommendation

Add a `RateLimiter<(PeerIndex, u32)>` to the `BlockFilter` struct, mirroring the pattern already used in `Relayer`. Check the rate limit in `try_process` before dispatching to `GetBlockFiltersProcess::execute()`, and return `StatusCode::TooManyRequests` (with optional peer ban) on violation.

### Proof of Concept

```rust
// Attacker: connect N peers, each sending GetBlockFilters(0) in a tight loop
let msg = packed::BlockFilterMessage::new_builder()
    .set(packed::GetBlockFilters::new_builder()
        .start_number(0u64)
        .build())
    .build();
loop {
    net.send(&node, SupportProtocols::Filter, msg.as_bytes());
    // no sleep — no server-side rate limit will stop this
}
```

Each iteration causes the server to read up to 1,000 blocks from RocksDB and allocate up to 1.8 MB. With N peers doing this concurrently, the node's async thread pool and memory are exhausted.

### Citations

**File:** sync/src/filter/get_block_filters_process.rs (L9-9)
```rust
const BATCH_SIZE: BlockNumber = 1000;
```

**File:** sync/src/filter/get_block_filters_process.rs (L45-57)
```rust
            for _ in 0..BATCH_SIZE {
                if let Some(block_hash) = active_chain.get_block_hash(block_number) {
                    if let Some(block_filter) = active_chain.get_block_filter(&block_hash) {
                        if current_content_size
                            + block_hash.as_slice().len()
                            + 4
                            + block_filter.as_slice().len()
                            + 4
                            >= (1.8 * 1024.0 * 1024.0) as usize
                        {
                            // Break if the encoded size of `block_hash` + `block_filter` + `start_number` + molecule header increase reaches 1.8MB, to avoid frame size too large
                            break;
                        }
```

**File:** sync/src/filter/get_block_filters_process.rs (L73-81)
```rust
            let content = packed::BlockFilters::new_builder()
                .start_number(start_number)
                .block_hashes(block_hashes)
                .filters(filters)
                .build();
            let message = packed::BlockFilterMessage::new_builder()
                .set(content)
                .build();
            async_send_message_to(&self.nc, self.peer, &message).await
```

**File:** sync/src/filter/mod.rs (L22-25)
```rust
pub struct BlockFilter {
    /// Sync shared state
    shared: Arc<SyncShared>,
}
```

**File:** sync/src/filter/mod.rs (L33-44)
```rust
    async fn try_process(
        &mut self,
        nc: Arc<dyn CKBProtocolContext + Sync>,
        peer: PeerIndex,
        message: packed::BlockFilterMessageUnionReader<'_>,
    ) -> Status {
        match message {
            packed::BlockFilterMessageUnionReader::GetBlockFilters(msg) => {
                GetBlockFiltersProcess::new(msg, self, nc, peer)
                    .execute()
                    .await
            }
```

**File:** sync/src/relayer/mod.rs (L63-67)
```rust
type RateLimiter<T> = governor::RateLimiter<
    T,
    governor::state::keyed::HashMapStateStore<T>,
    governor::clock::DefaultClock,
>;
```

**File:** sync/src/relayer/mod.rs (L78-82)
```rust
pub struct Relayer {
    chain: ChainController,
    pub(crate) shared: Arc<SyncShared>,
    rate_limiter: RateLimiter<(PeerIndex, u32)>,
}
```

**File:** sync/src/relayer/mod.rs (L88-99)
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

**File:** util/gen-types/schemas/extensions.mol (L211-213)
```text
struct GetBlockFilters {
    start_number:   Uint64,
}
```

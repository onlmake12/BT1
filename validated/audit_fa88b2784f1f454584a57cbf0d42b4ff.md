The code evidence is conclusive. Here is the assessment:

---

### Title
Missing Per-Peer Rate Limit in Filter Protocol Handler Enables I/O Exhaustion — (`sync/src/filter/mod.rs`, `sync/src/filter/get_block_filters_process.rs`)

### Summary
The `BlockFilter` protocol handler has no per-peer rate limiter. Any unprivileged peer connected via the Filter protocol can send `GetBlockFilters` messages in a tight loop, each triggering up to 1000 RocksDB reads and a ~1.8 MB molecule-encoded response, with no throttle applied. The `Relayer` and `HolePunching` protocol handlers both have explicit `governor`-based rate limiters; the `BlockFilter` handler has none.

### Finding Description

The `BlockFilter` struct holds only `shared: Arc<SyncShared>` — no rate limiter field: [1](#0-0) 

Its `try_process` dispatches directly to `GetBlockFiltersProcess::execute()` with no rate check: [2](#0-1) 

`GetBlockFiltersProcess::execute()` iterates up to `BATCH_SIZE = 1000` blocks, performing two DB reads per block (`get_block_hash` + `get_block_filter`) and accumulating up to 1.8 MB of response data per request: [3](#0-2) [4](#0-3) 

By contrast, the `Relayer` protocol explicitly installs a `governor` rate limiter keyed by `(PeerIndex, message_item_id)` at 30 req/s and checks it before every dispatch: [5](#0-4) [6](#0-5) 

The `HolePunching` protocol does the same at the `received` entry point: [7](#0-6) 

The `BlockFilter` handler has no equivalent guard anywhere in `sync/src/filter/`.

### Impact Explanation
A single attacker peer connecting via the Filter protocol can saturate the node's RocksDB read throughput and async task queue. Each request costs up to 2000 DB reads and ~1.8 MB of encoding work. At even modest send rates this monopolizes the I/O path shared with consensus-critical Relay and Sync protocol messages, causing processing delays or drops for those messages.

### Likelihood Explanation
The Filter protocol is optional but is enabled on nodes that serve light clients. The attack requires only a standard P2P connection — no credentials, no PoW, no stake. The attacker sends a fixed 9-byte `GetBlockFilters(start_number=0)` message in a loop. The gap is directly analogous to the one the `Relayer` rate limiter was added to close.

### Recommendation
Add a `governor`-based `RateLimiter<(PeerIndex, u32)>` field to `BlockFilter` (mirroring `Relayer::rate_limiter`) and check it at the top of `try_process` before dispatching any `GetBlockFilters`, `GetBlockFilterHashes`, or `GetBlockFilterCheckPoints` message.

### Proof of Concept
1. Enable block filter service; mine ≥1000 blocks so filters are built.
2. Connect a test peer using `SupportProtocols::Filter`.
3. Send `GetBlockFilters { start_number: 0 }` in a tight loop (no sleep).
4. Observe: each message triggers up to 2000 RocksDB reads and a ~1.8 MB `async_send_message_to` call.
5. Measure latency of concurrent Relay/Sync messages — assert they are delayed proportionally to the flood rate.
6. Assert no `TooManyRequests` status is ever returned by the Filter handler (confirmed by code: no such check exists). [8](#0-7)

### Citations

**File:** sync/src/filter/mod.rs (L22-25)
```rust
pub struct BlockFilter {
    /// Sync shared state
    shared: Arc<SyncShared>,
}
```

**File:** sync/src/filter/mod.rs (L39-44)
```rust
        match message {
            packed::BlockFilterMessageUnionReader::GetBlockFilters(msg) => {
                GetBlockFiltersProcess::new(msg, self, nc, peer)
                    .execute()
                    .await
            }
```

**File:** sync/src/filter/get_block_filters_process.rs (L9-9)
```rust
const BATCH_SIZE: BlockNumber = 1000;
```

**File:** sync/src/filter/get_block_filters_process.rs (L33-85)
```rust
    pub async fn execute(self) -> Status {
        let active_chain = self.filter.shared.active_chain();
        let start_number: BlockNumber = self.message.to_entity().start_number().into();
        let latest: BlockNumber = active_chain.get_latest_built_filter_block_number();

        if latest >= start_number {
            let mut block_hashes = Vec::new();
            let mut filters = Vec::new();
            let mut current_content_size = 0;
            current_content_size += 8; // Size of start_number
            current_content_size += 4 * 2; // Size of the header field `full-size` of `block_hash` and `block_filter`
            let mut block_number = start_number;
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
                        current_content_size +=
                            block_hash.as_slice().len() + block_filter.as_slice().len() + 4;
                        block_hashes.push(block_hash);
                        filters.push(block_filter);
                    } else {
                        break;
                    }
                } else {
                    break;
                }
                let Some(next_block_number) = block_number.checked_add(1) else {
                    break;
                };
                block_number = next_block_number;
            }
            let content = packed::BlockFilters::new_builder()
                .start_number(start_number)
                .block_hashes(block_hashes)
                .filters(filters)
                .build();
            let message = packed::BlockFilterMessage::new_builder()
                .set(content)
                .build();
            async_send_message_to(&self.nc, self.peer, &message).await
        } else {
            Status::ignored()
        }
    }
```

**File:** sync/src/relayer/mod.rs (L88-98)
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

**File:** network/src/protocols/hole_punching/mod.rs (L95-107)
```rust
        if self
            .rate_limiter
            .check_key(&(session_id, msg.item_id()))
            .is_err()
        {
            debug!(
                "process {} from {}; result is {}",
                item_name,
                session_id,
                status::StatusCode::TooManyRequests.with_context(msg.item_name())
            );
            return;
        }
```

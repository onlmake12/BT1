### Title
Missing Per-Peer Rate Limiting on `GetBlockFilters` Allows Resource Exhaustion — (`sync/src/filter/get_block_filters_process.rs`)

---

### Summary

The `BlockFilter` P2P protocol handler has no per-peer rate limiter, unlike the `Relayer` and `HolePunching` protocol handlers which both use `governor`-based rate limiters. Any unprivileged peer can send `GetBlockFilters` messages in a tight loop, causing the node to repeatedly execute up to 1,000 DB reads and allocate up to ~1.8 MB per response, with no throttling.

---

### Finding Description

`GetBlockFiltersProcess::execute` iterates up to `BATCH_SIZE = 1000` blocks, performing two DB lookups per block (`get_block_hash` + `get_block_filter`), accumulating results into `block_hashes` and `filters` Vecs, and serializing them into a molecule message capped at 1.8 MB before calling `async_send_message_to`. [1](#0-0) [2](#0-1) 

The 1.8 MB cap was introduced in PR #4972 solely to prevent tentacle frame-size disconnects, not as a security measure against flooding. [3](#0-2) 

The `BlockFilter` struct carries no `rate_limiter` field and the `received` → `process` → `try_process` call chain performs zero rate-limit checks before dispatching to `GetBlockFiltersProcess::execute`: [4](#0-3) [5](#0-4) 

By contrast, the `Relayer` protocol handler has an explicit `governor`-based rate limiter keyed by `(PeerIndex, message_type)` and returns `StatusCode::TooManyRequests` before any processing: [6](#0-5) [7](#0-6) 

The `HolePunching` protocol has the same pattern — a `rate_limiter` field checked at the top of `received`: [8](#0-7) [9](#0-8) 

`BlockFilter` is the only major P2P protocol handler with no equivalent guard.

---

### Impact Explanation

With the default `max_peers = 125`, up to 125 inbound peers can each send `GetBlockFilters(start_number=0)` in a tight loop. Each request triggers up to 2,000 DB reads and ~1.8 MB of heap allocation. Concurrently, this is up to 250,000 DB reads and ~225 MB of live allocations per "round," saturating the async thread pool, RocksDB I/O, and heap allocator. The node becomes unresponsive to legitimate peers and may OOM-crash on memory-constrained deployments. [10](#0-9) 

---

### Likelihood Explanation

The attack requires only a valid P2P connection — no PoW, no keys, no privileged role. The `Filter` protocol is listed in the default `support_protocols` and is therefore open to any peer on mainnet. [11](#0-10) 

---

### Recommendation

Add a `governor`-based `RateLimiter<(PeerIndex, u32)>` to `BlockFilter`, mirroring the pattern already used in `Relayer::new` (30 req/sec per peer per message type), and check it at the top of `BlockFilter::received` before dispatching to `try_process`. [12](#0-11) 

---

### Proof of Concept

1. Connect N peers (up to `max_peers`) to a node that has block filters built for a long chain.
2. Each peer sends `BlockFilterMessage { GetBlockFilters { start_number: 0 } }` in a tight loop over the `Filter` protocol.
3. Monitor node CPU, heap, and RocksDB I/O — all climb without bound.
4. Legitimate sync requests time out; node becomes unresponsive.

The call path is: `BlockFilter::received` → `BlockFilter::process` → `BlockFilter::try_process` → `GetBlockFiltersProcess::execute` (1,000 DB reads + ~1.8 MB alloc) → `async_send_message_to` — with no rate check anywhere in the chain. [13](#0-12) [14](#0-13)

### Citations

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

**File:** CHANGELOG.md (L124-126)
```markdown
- #4972: Limit the size of `filters` field in message `BlockFilters`  in `GetBlockFiltersProcess` (@Officeyutong)

    This PR limits size of `filters` field to 1.8MB, to avoid the frame sended by tentacle being too large (reaches 2MB) causing full CKB nodes disconnects light-client nodes
```

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

**File:** sync/src/filter/mod.rs (L122-153)
```rust
    async fn received(
        &mut self,
        nc: Arc<dyn CKBProtocolContext + Sync>,
        peer_index: PeerIndex,
        data: Bytes,
    ) {
        let msg = match packed::BlockFilterMessageReader::from_compatible_slice(&data) {
            Ok(msg) => msg.to_enum(),
            _ => {
                info_target!(
                    crate::LOG_TARGET_FILTER,
                    "Peer {} sends us a malformed message",
                    peer_index
                );
                nc.ban_peer(
                    peer_index,
                    BAD_MESSAGE_BAN_TIME,
                    String::from("send us a malformed message"),
                );
                return;
            }
        };

        debug_target!(
            crate::LOG_TARGET_FILTER,
            "received msg {} from {}",
            msg.item_name(),
            peer_index
        );
        let start_time = Instant::now();
        self.process(nc, peer_index, msg).await;
        debug_target!(
```

**File:** sync/src/relayer/mod.rs (L63-98)
```rust
type RateLimiter<T> = governor::RateLimiter<
    T,
    governor::state::keyed::HashMapStateStore<T>,
    governor::clock::DefaultClock,
>;

#[derive(Debug, Eq, PartialEq)]
pub enum ReconstructionResult {
    Block(BlockView),
    Missing(Vec<usize>, Vec<usize>),
    Collided,
    Error(Status),
}

/// Relayer protocol handle
pub struct Relayer {
    chain: ChainController,
    pub(crate) shared: Arc<SyncShared>,
    rate_limiter: RateLimiter<(PeerIndex, u32)>,
}

impl Relayer {
    /// Init relay protocol handle
    ///
    /// This is a runtime relay protocol shared state, and any relay messages will be processed and forwarded by it
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

**File:** network/src/protocols/hole_punching/mod.rs (L45-46)
```rust
    rate_limiter: RateLimiter<(PeerIndex, u32)>,
    forward_rate_limiter: RateLimiter<(PeerId, PeerId, u32)>,
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

**File:** resource/ckb.toml (L94-94)
```text
max_peers = 125
```

**File:** resource/ckb.toml (L112-112)
```text
support_protocols = ["Ping", "Discovery", "Identify", "Feeler", "DisconnectMessage", "Sync", "Relay", "Time", "Alert", "LightClient", "Filter", "HolePunching"]
```

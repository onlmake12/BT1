The code tells a clear story. Let me lay out the findings precisely.

### Title
Missing Per-Peer Rate Limiting in `BlockFilter` Protocol Allows Cheap I/O Exhaustion — (`sync/src/filter/mod.rs`, `sync/src/filter/get_block_filters_process.rs`)

---

### Summary

The `BlockFilter` protocol handler has no per-peer rate limiter. An unprivileged attacker can send a continuous stream of valid, 12-byte `GetBlockFilters` messages, each of which causes the server to perform up to **2,000 sequential database reads** (up to 1,000 × `get_block_hash` + `get_block_filter`). No punishment is applied for this pattern. The `Relayer` and `HolePunching` protocols both have explicit `governor`-based rate limiters; `BlockFilter` has none.

---

### Finding Description

**Message schema** — `GetBlockFilters` is a fixed-size Molecule struct with a single field:

```
struct GetBlockFilters {
    start_number: Uint64,
}
```

Total wire size: ~12 bytes. [1](#0-0) 

**Server-side work per message** — `GetBlockFiltersProcess::execute()` loops up to `BATCH_SIZE = 1000` times, calling `get_block_hash(block_number)` and `get_block_filter(&block_hash)` on every iteration — up to 2,000 DB reads per message: [2](#0-1) [3](#0-2) 

**No rate limiter in `BlockFilter`** — The `BlockFilter` struct holds only `shared: Arc<SyncShared>`. There is no `rate_limiter` field, no `governor` import, and no per-peer message-count check anywhere in `sync/src/filter/`: [4](#0-3) 

The `received()` handler bans malformed (unparseable) messages immediately, but a well-formed `GetBlockFilters` with any `start_number` passes straight through to `execute()` with zero throttling: [5](#0-4) 

**Contrast with other protocols** — Both `Relayer` and `HolePunching` carry an explicit `rate_limiter: RateLimiter<(PeerIndex, u32)>` (30 req/sec per peer per message type) and check it before dispatching: [6](#0-5) [7](#0-6) [8](#0-7) [9](#0-8) 

`BlockFilter` has no equivalent. The omission is confirmed by a grep for `rate_limiter|RateLimiter|governor` in `sync/src/filter/**` returning zero matches.

---

### Impact Explanation

Each 12-byte `GetBlockFilters` message forces the full node to execute up to 2,000 RocksDB point-reads. An attacker maintaining a single TCP connection can sustain thousands of such messages per second, saturating the node's I/O and async executor threads. This degrades or halts block/header sync for all legitimate peers sharing the same node, constituting network congestion at minimal attacker cost — matching the stated High impact category.

---

### Likelihood Explanation

The attack requires only a standard P2P connection to a full node that has the Filter protocol enabled. No key material, privileged role, or hashpower is needed. The message is trivially constructable. The absence of any rate-limiting guard makes the path fully reliable and repeatable.

---

### Recommendation

Add a `governor`-based `RateLimiter<(PeerIndex, u32)>` to `BlockFilter` (mirroring `Relayer::rate_limiter`) and check it in `received()` before dispatching to `try_process()`. A quota of ~30 `GetBlockFilters` requests per second per peer is consistent with the existing policy in the relay and hole-punching protocols.

---

### Proof of Concept

```rust
// Attacker loop (pseudocode):
loop {
    let msg = packed::GetBlockFilters::new_builder()
        .start_number(0u64)   // any valid start_number
        .build();
    let envelope = packed::BlockFilterMessage::new_builder().set(msg).build();
    send_to_victim(SupportProtocols::Filter, envelope.as_bytes()); // ~12 bytes
    // No sleep — no server-side throttle will stop this
}
// Each iteration forces the victim to do up to 2,000 DB reads.
// No ban is ever triggered for well-formed messages.
```

The victim node's I/O thread pool saturates; legitimate sync peers experience stalled header/block delivery.

### Citations

**File:** util/gen-types/schemas/extensions.mol (L211-213)
```text
struct GetBlockFilters {
    start_number:   Uint64,
}
```

**File:** sync/src/filter/get_block_filters_process.rs (L9-9)
```rust
const BATCH_SIZE: BlockNumber = 1000;
```

**File:** sync/src/filter/get_block_filters_process.rs (L45-67)
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
```

**File:** sync/src/filter/mod.rs (L21-25)
```rust
#[derive(Clone)]
pub struct BlockFilter {
    /// Sync shared state
    shared: Arc<SyncShared>,
}
```

**File:** sync/src/filter/mod.rs (L122-160)
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
            crate::LOG_TARGET_FILTER,
            "process message={}, peer={}, cost={:?}",
            msg.item_name(),
            peer_index,
            Instant::now().saturating_duration_since(start_time),
        );
    }
```

**File:** sync/src/relayer/mod.rs (L81-98)
```rust
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

**File:** network/src/protocols/hole_punching/mod.rs (L45-47)
```rust
    rate_limiter: RateLimiter<(PeerIndex, u32)>,
    forward_rate_limiter: RateLimiter<(PeerId, PeerId, u32)>,
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

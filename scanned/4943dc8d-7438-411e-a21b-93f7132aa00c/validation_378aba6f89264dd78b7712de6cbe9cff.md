The code confirms this is a real, reachable vulnerability. Here is the analysis:

**Key facts from the code:**

1. `GetBlockFiltersProcess::execute` iterates up to `BATCH_SIZE=1000`, doing two DB reads per iteration (`get_block_hash` + `get_block_filter`) and sends a response capped at ~1.8MB. [1](#0-0) [2](#0-1) 

2. The `BlockFilter` handler's `received()` method has **zero rate limiting** — it directly dispatches to `self.process(...)` with no per-peer throttle. [3](#0-2) 

3. By contrast, the `Relayer` handler explicitly constructs a `RateLimiter` (30 req/s per peer+message type) and gates every non-PoW message through it. [4](#0-3) 

4. The `BlockFilter` struct holds only `shared: Arc<SyncShared>` — no rate limiter field exists. [5](#0-4) 

---

### Title
Unbounded `GetBlockFilters` Request Rate Allows Single Unauthenticated Peer to Exhaust Outbound Bandwidth — (`sync/src/filter/get_block_filters_process.rs`)

### Summary
The `BlockFilter` P2P protocol handler applies no per-peer rate limit on `GetBlockFilters` messages. A single unauthenticated remote peer can repeatedly send `GetBlockFilters(start_number=0)`, each triggering up to 1000 ChainDB reads and a ~1.8 MB response, with no throttle, saturating the node's outbound bandwidth and async task queue.

### Finding Description
`GetBlockFiltersProcess::execute` loops up to `BATCH_SIZE=1000` times, calling `active_chain.get_block_hash(block_number)` (snapshot read) and `active_chain.get_block_filter(&block_hash)` (live ChainDB read) per iteration, then serializes and sends a response up to 1.8 MB via `async_send_message_to`. [6](#0-5) 

The `BlockFilter::received` handler dispatches directly to `self.process(...)` with no rate-limit check: [7](#0-6) 

The `Relayer` handler, by contrast, has an explicit `RateLimiter::hashmap` keyed by `(peer, message_item_id)` and returns `TooManyRequests` when exceeded: [8](#0-7) 

This asymmetry means the Filter protocol is unprotected while the Relay protocol is protected.

### Impact Explanation
A single unauthenticated peer can monopolize the node's outbound bandwidth. At, say, 100 requests/second, the node would attempt to send ~180 MB/s to that one peer. This saturates the send queue, starves legitimate peers (including other light clients and full nodes), and can exhaust async task capacity. The 1.8 MB cap per response (added in PR #4972) prevents frame-level disconnects but does not limit request frequency. [9](#0-8) 

### Likelihood Explanation
The attack requires only a valid P2P connection and the ability to send a well-formed `GetBlockFilters` message with any `start_number`. No authentication, PoW, or special privilege is needed. The `GetBlockFilters` struct is a single `Uint64` field, trivial to construct at high frequency. [10](#0-9) 

### Recommendation
Add a per-peer rate limiter to `BlockFilter`, mirroring the pattern in `Relayer`:
- Add a `RateLimiter<(PeerIndex, u8)>` field to the `BlockFilter` struct.
- In `try_process`, before dispatching `GetBlockFilters` (and `GetBlockFilterHashes`), call `rate_limiter.check_key(&(peer, message.item_id()))` and return `StatusCode::TooManyRequests` on failure.
- A quota of ~10 req/s per peer per message type is consistent with the existing relay policy. [11](#0-10) 

### Proof of Concept
1. Connect a single peer to a CKB node with ≥1000 blocks with filters built.
2. In a tight loop, send `GetBlockFilters { start_number: 0 }` over the Filter protocol.
3. Measure bytes received per second from the node on that connection.
4. Assert that bytes/second exceeds any reasonable per-peer cap (e.g., >10 MB/s is achievable on localhost).
5. Observe that legitimate peers on the same node receive degraded or no responses during the flood.

### Citations

**File:** sync/src/filter/get_block_filters_process.rs (L9-9)
```rust
const BATCH_SIZE: BlockNumber = 1000;
```

**File:** sync/src/filter/get_block_filters_process.rs (L33-36)
```rust
    pub async fn execute(self) -> Status {
        let active_chain = self.filter.shared.active_chain();
        let start_number: BlockNumber = self.message.to_entity().start_number().into();
        let latest: BlockNumber = active_chain.get_latest_built_filter_block_number();
```

**File:** sync/src/filter/get_block_filters_process.rs (L45-81)
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

**File:** sync/src/relayer/mod.rs (L89-123)
```rust
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

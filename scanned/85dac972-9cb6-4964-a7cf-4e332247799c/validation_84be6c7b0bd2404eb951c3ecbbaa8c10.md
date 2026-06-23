### Title
Unbounded RocksDB Read Amplification via Unauthenticated `GetBlockFilterHashes` Flood — (`sync/src/filter/get_block_filter_hashes_process.rs`)

---

### Summary

The `BlockFilter` protocol handler has no per-peer rate limiting. Any unauthenticated peer can send `GetBlockFilterHashes(start_number=0)` in a tight loop, triggering up to 4 000 RocksDB reads per message (2 000 `get_block_hash` + 2 000 `get_block_filter_hash`), with no throttle at any layer. The `Relayer` protocol explicitly carries a 30 req/s governor; `BlockFilter` carries none. The asymmetry is an oversight, not a design choice.

---

### Finding Description

**`BATCH_SIZE`** is set to 2 000 in `get_block_filter_hashes_process.rs`: [1](#0-0) 

The `execute()` loop iterates up to that limit, issuing two RocksDB reads per iteration: [2](#0-1) 

`BlockFilter` carries no `rate_limiter` field: [3](#0-2) 

`BlockFilter::received()` performs zero rate checks before dispatching: [4](#0-3) 

`BlockFilter::try_process()` also performs zero rate checks: [5](#0-4) 

A `grep` across all of `sync/src/**/*.rs` for `rate_limiter|RateLimiter|governor` returns matches **only** in `sync/src/relayer/mod.rs` — zero hits in any filter file.

By contrast, `Relayer` explicitly initialises a 30 req/s per-peer governor: [6](#0-5) 

And enforces it at the top of `try_process()`: [7](#0-6) 

The same missing-rate-limit pattern also affects `GetBlockFilters` (BATCH\_SIZE 1 000, two reads per step) and `GetBlockFilterCheckPoints` (BATCH\_SIZE 2 000): [8](#0-7) [9](#0-8) 

The network-layer `CKBHandler::received` wrapper adds no independent throttle either: [10](#0-9) 

---

### Impact Explanation

A single attacker peer, at negligible bandwidth cost (each `GetBlockFilterHashes` message is ~12 bytes), can sustain thousands of RocksDB random-read IOPS on the victim node. Because `BlockFilter::received` is `async` and awaited inline, the async executor's task queue fills with long-running DB work, starving all other protocol handlers (sync, relay, RPC) of scheduling time. The node becomes unresponsive to legitimate peers without the attacker ever needing PoW, stake, or any privileged role.

---

### Likelihood Explanation

The attack requires only a valid TCP connection to the Filter protocol port — no authentication, no prior state, no special capability. The message is trivially constructable. The contrast with `Relayer`'s explicit governor confirms the developers are aware of the pattern; its absence from `BlockFilter` is a concrete oversight rather than an intentional trade-off.

---

### Recommendation

Add a per-peer, per-message-type `governor::RateLimiter` to `BlockFilter`, mirroring the pattern already used in `Relayer`:

1. Add `rate_limiter: RateLimiter<(PeerIndex, u32)>` to the `BlockFilter` struct.
2. In `BlockFilter::new`, initialise it with `governor::Quota::per_second(NonZeroU32::new(30).unwrap())` (or a tighter value appropriate for filter queries).
3. At the top of `BlockFilter::try_process`, call `self.rate_limiter.check_key(&(peer, message.item_id()))` and return `StatusCode::TooManyRequests` on failure.
4. Call `self.rate_limiter.retain_recent()` in `BlockFilter::disconnected`.
5. Apply the same fix to `GetBlockFilters` and `GetBlockFilterCheckPoints` handlers, which share the same structural gap.

---

### Proof of Concept

```
1. Connect a mock peer to the CKB Filter protocol (SupportProtocols::Filter).
2. In a tight loop, send:
     GetBlockFilterHashes { start_number: 0 }
   (serialised as a valid BlockFilterMessage molecule struct, ~12 bytes each).
3. Observe on the victim node:
   - RocksDB read IOPS spike to N_messages × 4 000 per second.
   - Tokio async task queue depth grows unboundedly.
   - Other peers' Sync/Relay messages are not processed (latency → timeout).
   - Node appears unresponsive to block propagation and transaction relay.
4. Assert: no ban, no disconnect, no rate-limit response is ever returned
   by BlockFilter::received or BlockFilter::try_process.
```

### Citations

**File:** sync/src/filter/get_block_filter_hashes_process.rs (L8-8)
```rust
const BATCH_SIZE: BlockNumber = 2000;
```

**File:** sync/src/filter/get_block_filter_hashes_process.rs (L53-66)
```rust
            for _ in 0..BATCH_SIZE {
                if let Some(block_filter_hash) = active_chain
                    .get_block_hash(block_number)
                    .and_then(|block_hash| active_chain.get_block_filter_hash(&block_hash))
                {
                    block_filter_hashes.push(block_filter_hash);
                } else {
                    break;
                }
                let Some(next_block_number) = block_number.checked_add(1) else {
                    break;
                };
                block_number = next_block_number;
            }
```

**File:** sync/src/filter/mod.rs (L22-25)
```rust
pub struct BlockFilter {
    /// Sync shared state
    shared: Arc<SyncShared>,
}
```

**File:** sync/src/filter/mod.rs (L33-68)
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
            packed::BlockFilterMessageUnionReader::GetBlockFilterHashes(msg) => {
                GetBlockFilterHashesProcess::new(msg, self, nc, peer)
                    .execute()
                    .await
            }
            packed::BlockFilterMessageUnionReader::GetBlockFilterCheckPoints(msg) => {
                GetBlockFilterCheckPointsProcess::new(msg, self, nc, peer)
                    .execute()
                    .await
            }
            packed::BlockFilterMessageUnionReader::BlockFilters(_)
            | packed::BlockFilterMessageUnionReader::BlockFilterHashes(_)
            | packed::BlockFilterMessageUnionReader::BlockFilterCheckPoints(_) => {
                // remote peer should not send block filter to us without asking
                // TODO: ban remote peer
                warn_target!(
                    crate::LOG_TARGET_FILTER,
                    "Received unexpected message from peer: {:?}",
                    peer
                );
                Status::ignored()
            }
        }
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

**File:** sync/src/filter/get_block_filters_process.rs (L9-9)
```rust
const BATCH_SIZE: BlockNumber = 1000;
```

**File:** sync/src/filter/get_block_filter_check_points_process.rs (L9-10)
```rust
const BATCH_SIZE: BlockNumber = 2000;
const CHECK_POINT_INTERVAL: BlockNumber = 2000;
```

**File:** network/src/protocols/mod.rs (L365-383)
```rust
    async fn received(&mut self, context: ProtocolContextMutRef<'_>, data: Bytes) {
        if !self.network_state.is_active() {
            return;
        }

        trace!(
            "[received message]: {}, {}, length={}",
            self.proto_id,
            context.session.id,
            data.len()
        );
        let nc = DefaultCKBProtocolContext {
            proto_id: self.proto_id,
            network_state: Arc::clone(&self.network_state),
            p2p_control: context.control().to_owned().into(),
            async_p2p_control: context.control().to_owned(),
        };
        let peer_index = context.session.id;
        self.handler.received(Arc::new(nc), peer_index, data).await;
```

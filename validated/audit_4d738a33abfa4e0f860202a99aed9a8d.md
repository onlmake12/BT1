### Title
Unbounded `GetBlockFilterCheckPoints` Flood Causes Head-of-Line Blocking in Filter Protocol Handler — (`sync/src/filter/mod.rs`, `sync/src/filter/get_block_filter_check_points_process.rs`)

---

### Summary

The `BlockFilter` protocol handler processes all incoming messages from all peers sequentially in a single async task with no per-peer rate limiting. A single unprivileged peer can flood `GetBlockFilterCheckPoints` messages, each triggering up to 4,000 sequential DB reads, monopolizing the handler and starving legitimate light-client peers of `GetBlockFilterHashes` and `GetBlockFilters` responses.

---

### Finding Description

**Entrypoint:** Any peer connecting on the Filter protocol (`SupportProtocols::Filter`) can send arbitrary `GetBlockFilterCheckPoints` messages.

**Sequential handler model:** `CKBHandler::received` in `network/src/protocols/mod.rs` calls `self.handler.received(...).await` — the handler takes `&mut self`, so tentacle dispatches messages from all peers through the same single async context, one at a time. [1](#0-0) 

**No `.await` yield between peers:** `BlockFilter::received` fully awaits `self.process(...)` before returning, meaning the next message (from any peer) cannot begin until the current one completes. [2](#0-1) 

**Per-message cost:** `GetBlockFilterCheckPointsProcess::execute` loops up to `BATCH_SIZE = 2000` iterations, each performing two DB reads (`get_block_hash` + `get_block_filter_hash`), totalling up to 4,000 synchronous DB reads per message. [3](#0-2) 

**No rate limiting in `BlockFilter`:** The `BlockFilter` struct has no `rate_limiter` field and no per-peer quota check anywhere in `try_process`. This is in direct contrast to:

- `Relayer`, which has `rate_limiter: RateLimiter<(PeerIndex, u32)>` enforcing 30 req/sec per peer per message type: [4](#0-3) 

- `HolePunching`, which has both `rate_limiter` and `forward_rate_limiter` checked before any processing: [5](#0-4) 

The `BlockFilter` handler has neither: [6](#0-5) 

---

### Impact Explanation

A single attacker peer sends a continuous stream of `GetBlockFilterCheckPoints` messages (e.g., with `start_number = 0, 2000, 4000, ...`). Each message occupies the handler for the duration of up to 4,000 DB reads. Because the handler is sequential and shared across all peers, legitimate light-client peers sending `GetBlockFilterHashes` or `GetBlockFilters` are queued behind the flood. Their requests are delayed proportionally to the flood rate, effectively preventing them from syncing filter data. Light clients relying on filter data for transaction monitoring cannot function during the attack, causing economic harm to users who depend on light clients.

---

### Likelihood Explanation

The attack requires only a single TCP connection to a CKB node with the Filter protocol enabled. No special privileges, keys, or hashpower are needed. The absence of rate limiting is a concrete, verifiable code gap. The attack is locally reproducible with two mock peers.

---

### Recommendation

Add a per-peer, per-message-type rate limiter to `BlockFilter`, mirroring the pattern already used in `Relayer`:

```rust
pub struct BlockFilter {
    shared: Arc<SyncShared>,
    rate_limiter: RateLimiter<(PeerIndex, u32)>,
}
```

Check the limiter at the top of `try_process` before dispatching to any `*Process::execute`. A quota of ~5–10 `GetBlockFilterCheckPoints` requests per second per peer is sufficient for legitimate light-client use.

---

### Proof of Concept

1. Connect two mock peers (peer A and peer B) to a CKB node with the Filter protocol.
2. Have peer A send `GetBlockFilterCheckPoints` messages in a tight loop (`start_number = 0, 2000, 4000, ...`).
3. Have peer B send `GetBlockFilterHashes` messages at a steady rate.
4. Measure peer B's response latency with and without peer A's flood.
5. Assert that peer B's latency increases proportionally to peer A's flood rate, confirming head-of-line blocking.

The root cause is confirmed at: [7](#0-6) [8](#0-7)

### Citations

**File:** network/src/protocols/mod.rs (L365-384)
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
    }
```

**File:** sync/src/filter/mod.rs (L22-68)
```rust
pub struct BlockFilter {
    /// Sync shared state
    shared: Arc<SyncShared>,
}

impl BlockFilter {
    /// Create a new block filter protocol handler
    pub fn new(shared: Arc<SyncShared>) -> Self {
        Self { shared }
    }

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

**File:** sync/src/filter/mod.rs (L151-153)
```rust
        let start_time = Instant::now();
        self.process(nc, peer_index, msg).await;
        debug_target!(
```

**File:** sync/src/filter/get_block_filter_check_points_process.rs (L9-69)
```rust
const BATCH_SIZE: BlockNumber = 2000;
const CHECK_POINT_INTERVAL: BlockNumber = 2000;

pub struct GetBlockFilterCheckPointsProcess<'a> {
    message: packed::GetBlockFilterCheckPointsReader<'a>,
    filter: &'a BlockFilter,
    nc: Arc<dyn CKBProtocolContext + Sync>,
    peer: PeerIndex,
}

impl<'a> GetBlockFilterCheckPointsProcess<'a> {
    pub fn new(
        message: packed::GetBlockFilterCheckPointsReader<'a>,
        filter: &'a BlockFilter,
        nc: Arc<dyn CKBProtocolContext + Sync>,
        peer: PeerIndex,
    ) -> Self {
        Self {
            message,
            nc,
            filter,
            peer,
        }
    }

    pub async fn execute(self) -> Status {
        let active_chain = self.filter.shared.active_chain();
        let start_number: BlockNumber = self.message.to_entity().start_number().into();
        let latest: BlockNumber = active_chain.get_latest_built_filter_block_number();

        let mut block_filter_hashes = Vec::new();

        if latest >= start_number {
            let mut block_number = start_number;
            for _ in 0..BATCH_SIZE {
                if let Some(block_filter_hash) = active_chain
                    .get_block_hash(block_number)
                    .and_then(|block_hash| active_chain.get_block_filter_hash(&block_hash))
                {
                    block_filter_hashes.push(block_filter_hash);
                } else {
                    break;
                }
                let Some(next_block_number) = block_number.checked_add(CHECK_POINT_INTERVAL) else {
                    break;
                };
                block_number = next_block_number;
            }
            let content = packed::BlockFilterCheckPoints::new_builder()
                .start_number(start_number)
                .block_filter_hashes(block_filter_hashes)
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

**File:** sync/src/relayer/mod.rs (L81-123)
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

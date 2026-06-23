### Title
Missing Per-Peer Rate Limit in Filter Protocol Allows DB Read Amplification and Bandwidth Exhaustion — (`sync/src/filter/get_block_filter_hashes_process.rs`)

---

### Summary

The `BlockFilter` protocol handler has no per-peer rate limiter, unlike both `Relayer` and `HolePunching` which enforce 30 req/sec per `(PeerIndex, message_type)`. A single unprivileged remote peer can flood the node with `GetBlockFilterHashes` messages, each triggering up to 2000 sequential DB lookups and a ~64 KB serialized response, with no throttle, no ban, and no backpressure.

---

### Finding Description

`GetBlockFilterHashesProcess::execute` iterates up to `BATCH_SIZE = 2000` blocks, calling `get_block_hash` + `get_block_filter_hash` for each, then serializes and sends the full result back to the requesting peer. [1](#0-0) [2](#0-1) 

The `BlockFilter` struct holds no `rate_limiter` field and `try_process` dispatches directly to the handler with zero rate checks: [3](#0-2) [4](#0-3) 

By contrast, `Relayer` carries an explicit `rate_limiter: RateLimiter<(PeerIndex, u32)>` initialized at 30 req/sec and checks it before every handler dispatch: [5](#0-4) [6](#0-5) 

`HolePunching` applies the same pattern at the `received` entry point before any processing: [7](#0-6) [8](#0-7) 

A grep for `rate_limiter` scoped to `sync/src/filter/` returns zero matches — the guard is entirely absent from the Filter protocol. [9](#0-8) 

`GetBlockFilterCheckPointsProcess` and `GetBlockFiltersProcess` share the same pattern and are equally unguarded. [10](#0-9) [11](#0-10) 

---

### Impact Explanation

Each `GetBlockFilterHashes(start_number=0)` request from a single peer causes:
- Up to **4000 RocksDB reads** (2000 × `get_block_hash` + 2000 × `get_block_filter_hash`)
- A **~64 KB outbound response** (2000 × 32-byte hashes + framing)

With no rate limit, a single peer connection can sustain this indefinitely. A handful of attacker connections can saturate the node's DB read throughput and outbound bandwidth without triggering any ban or `TooManyRequests` status.

---

### Likelihood Explanation

The attack requires only a valid P2P connection and a node with at least one block of built filter data (the precondition `latest >= start_number` at line 39 must hold). No PoW, no keys, no special privileges. The message is tiny (8 bytes for `start_number`), so the attacker's inbound cost is negligible relative to the node's outbound cost — a classic amplification ratio.

---

### Recommendation

Add a `RateLimiter<(PeerIndex, u32)>` field to `BlockFilter` (mirroring `Relayer::rate_limiter`) and check it at the top of `try_process` before dispatching to any of the three filter handlers. The existing 30 req/sec quota used by `Relayer` and `HolePunching` is a reasonable starting point.

---

### Proof of Concept

1. Connect a single peer to a node that has built at least 1 block of filter data.
2. In a tight loop, send `GetBlockFilterHashes { start_number: 0 }` 1000 times over the Filter protocol.
3. Observe: the node performs ~4,000,000 DB reads and sends ~64 MB of responses.
4. Assert: no `TooManyRequests` status is returned, no ban is issued, and the node's DB read metrics spike proportionally to the request rate.

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

**File:** sync/src/filter/get_block_filter_check_points_process.rs (L34-68)
```rust
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

### Title
Unbounded Per-Peer DB Read Amplification in Filter Protocol — Missing Rate Limiter in `GetBlockFiltersProcess::execute` - (`sync/src/filter/get_block_filters_process.rs`)

---

### Summary

The Filter protocol handler (`sync/src/filter/mod.rs`) processes `GetBlockFilters` messages with no per-peer rate limiter. Each message triggers up to **2,000 RocksDB reads** (1,000 × `get_block_hash` + 1,000 × `get_block_filter`). The Relay protocol has an explicit governor-based 30 req/s limiter keyed by `(PeerIndex, message_type)`; the Filter protocol has none. Any unprivileged inbound peer can send `GetBlockFilters(start_number=0)` in a tight loop, and N such peers multiply the I/O amplification linearly.

---

### Finding Description

**Step 1 — Entrypoint.**
Any peer that connects on the Filter protocol (enabled by default in `support_protocols`) can send a `GetBlockFilters` message. The message is a 9-byte struct containing only `start_number: Uint64`. [1](#0-0) 

**Step 2 — Dispatch with no rate check.**
`BlockFilter::received` deserializes the message and calls `self.process(...)`. `process` calls `try_process`, which dispatches to `GetBlockFiltersProcess::new(...).execute().await`. There is no rate-limiter field on `BlockFilter`, no governor check, and no per-peer accounting of any kind. [2](#0-1) 

Confirmed by grep: zero occurrences of `rate_limiter`, `governor`, or `RateLimiter` anywhere under `sync/src/filter/`.

**Step 3 — Up to 2,000 RocksDB reads per message.**
`GetBlockFiltersProcess::execute` loops up to `BATCH_SIZE = 1000` times. Each iteration calls:
- `active_chain.get_block_hash(block_number)` → RocksDB read on `COLUMN_INDEX` ("0")
- `active_chain.get_block_filter(&block_hash)` → RocksDB read on `COLUMN_BLOCK_FILTER` ("17") [3](#0-2) 

The 1.8 MB size cap may terminate the loop early for blocks with large filters, but for early mainnet blocks (small filters), all 1,000 iterations complete, yielding 2,000 reads per message.

**Step 4 — `GetBlockFilterHashes` is worse.**
`GetBlockFilterHashesProcess` has `BATCH_SIZE = 2000` and also performs 2 reads per iteration (plus 1 extra for the parent hash), for up to **4,001 reads per message**. [4](#0-3) 

**Step 5 — Contrast with Relay protocol.**
The Relay handler explicitly constructs a governor-based rate limiter at 30 req/s keyed by `(PeerIndex, message_type)` and checks it before every dispatch: [5](#0-4) 

The Filter protocol has no equivalent guard.

**Step 6 — Peer count amplification.**
Default config allows up to 125 total peers (`max_peers = 125`, `max_outbound_peers = 8`), meaning up to ~117 inbound peers. [6](#0-5) 

Each peer sending `GetBlockFilters(start_number=0)` at maximum rate contributes 2,000 reads/message. With N peers, the node sustains N × 2,000 reads per message-processing round.

---

### Impact Explanation

Sustained high-rate `GetBlockFilters(start_number=0)` messages from N peers cause:
- **RocksDB I/O saturation**: 2,000 reads per message × N peers × message rate saturates disk I/O, degrading block sync, tx-pool, and all other DB-dependent operations.
- **Async executor thread starvation**: Each `execute()` call holds an async task for the full loop duration, starving other protocol handlers.
- **Legitimate peer service degradation**: Honest light clients and sync peers experience increased latency or timeouts.

The attack requires only establishing inbound connections (no PoW, no stake, no privileged access) and sending a 9-byte message repeatedly.

---

### Likelihood Explanation

- The Filter protocol is enabled by default in production `ckb.toml`.
- The attack message is trivially constructable (9 bytes, no validation beyond deserialization).
- The contrast with the Relay protocol's rate limiter confirms this is an unintentional omission, not a design choice.
- The CHANGELOG entry `#4972` (limiting response size to 1.8 MB) shows the team has already patched one amplification vector in this handler but left the request rate unbounded. [7](#0-6) 

---

### Recommendation

Add a governor-based rate limiter to `BlockFilter`, mirroring the Relay protocol:

1. Add `rate_limiter: RateLimiter<(PeerIndex, u32)>` to the `BlockFilter` struct.
2. In `try_process`, check `self.rate_limiter.check_key(&(peer, message.item_id()))` before dispatching to any process handler.
3. Apply the same 30 req/s quota (or a tighter one, given the higher per-message cost of filter reads vs. relay messages).
4. Consider also applying the rate limiter to `GetBlockFilterHashes` (BATCH_SIZE=2000, up to 4001 reads/msg) and `GetBlockFilterCheckPoints`.

---

### Proof of Concept

```
1. Connect N peers (e.g., N=50) to a target CKB full node with block filter enabled.
2. Each peer sends in a tight loop:
     GetBlockFilters { start_number: 0 }
   (9-byte message, no PoW or auth required)
3. Observe on the target node:
   - RocksDB read IOPS spike to N × ~2000 reads/round
   - CPU usage on async executor threads increases
   - Sync latency for honest peers degrades
   - Node I/O wait increases proportionally with N
4. Compare against a patched node with a 30 req/s rate limiter:
   - IOPS capped at N × 30 × 2000 reads/s regardless of send rate
```

The attack is locally reproducible using the existing integration test harness (`test/src/specs/sync/block_filter.rs`) as a template for the peer connection and message-sending logic. [8](#0-7) [9](#0-8)

### Citations

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

**File:** sync/src/filter/mod.rs (L118-160)
```rust
#[async_trait]
impl CKBProtocolHandler for BlockFilter {
    async fn init(&mut self, _nc: Arc<dyn CKBProtocolContext + Sync>) {}

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

**File:** sync/src/filter/get_block_filters_process.rs (L9-85)
```rust
const BATCH_SIZE: BlockNumber = 1000;

pub struct GetBlockFiltersProcess<'a> {
    message: packed::GetBlockFiltersReader<'a>,
    filter: &'a BlockFilter,
    nc: Arc<dyn CKBProtocolContext + Sync>,
    peer: PeerIndex,
}

impl<'a> GetBlockFiltersProcess<'a> {
    pub fn new(
        message: packed::GetBlockFiltersReader<'a>,
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

**File:** sync/src/filter/get_block_filter_hashes_process.rs (L8-66)
```rust
const BATCH_SIZE: BlockNumber = 2000;

pub struct GetBlockFilterHashesProcess<'a> {
    message: packed::GetBlockFilterHashesReader<'a>,
    filter: &'a BlockFilter,
    nc: Arc<dyn CKBProtocolContext + Sync>,
    peer: PeerIndex,
}

impl<'a> GetBlockFilterHashesProcess<'a> {
    pub fn new(
        message: packed::GetBlockFilterHashesReader<'a>,
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
            let parent_block_filter_hash = if start_number > 0 {
                match active_chain
                    .get_block_hash(start_number - 1)
                    .and_then(|block_hash| active_chain.get_block_filter_hash(&block_hash))
                {
                    Some(parent_block_filter_hash) => parent_block_filter_hash,
                    None => return Status::ignored(),
                }
            } else {
                packed::Byte32::zero()
            };

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
                let Some(next_block_number) = block_number.checked_add(1) else {
                    break;
                };
                block_number = next_block_number;
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

**File:** resource/ckb.toml (L94-95)
```text
max_peers = 125
max_outbound_peers = 8
```

**File:** CHANGELOG.md (L124-126)
```markdown
- #4972: Limit the size of `filters` field in message `BlockFilters`  in `GetBlockFiltersProcess` (@Officeyutong)

    This PR limits size of `filters` field to 1.8MB, to avoid the frame sended by tentacle being too large (reaches 2MB) causing full CKB nodes disconnects light-client nodes
```

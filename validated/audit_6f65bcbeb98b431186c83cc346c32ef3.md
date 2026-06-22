### Title
Missing Per-Peer Rate Limit on `GetBlockFilterCheckPoints` Enables DB Read Amplification — (`sync/src/filter/get_block_filter_check_points_process.rs`)

---

### Summary

The `BlockFilter` protocol handler has no rate limiter. Any connected peer can flood the node with `GetBlockFilterCheckPoints(start_number=0)` messages, each of which unconditionally triggers up to 4,000 RocksDB reads (2,000 × `get_block_hash` + 2,000 × `get_block_filter_hash`) with no throttling, saturating the node's storage I/O at negligible attacker cost.

---

### Finding Description

`GetBlockFilterCheckPointsProcess::execute` iterates up to `BATCH_SIZE = 2000` times, performing two DB reads per iteration: [1](#0-0) [2](#0-1) 

The outer `BlockFilter` handler dispatches directly to this function with no rate-limit check: [3](#0-2) [4](#0-3) 

The `BlockFilter` struct carries only `shared: Arc<SyncShared>` — there is no `rate_limiter` field and no call to one anywhere in `sync/src/filter/`: [3](#0-2) 

This is in direct contrast to the `Relayer` handler, which holds a keyed `RateLimiter<(PeerIndex, u32)>` and gates every non-PoW message through it before dispatch: [5](#0-4) [6](#0-5) 

---

### Impact Explanation

On a node with filter data built for a long chain, each `GetBlockFilterCheckPoints(start_number=0)` message causes the node to execute the full 2,000-iteration loop, issuing up to 4,000 synchronous RocksDB point-reads before returning. An attacker who streams these messages continuously from a single peer connection saturates the node's disk I/O, degrading its ability to process sync, relay, and block-propagation messages — causing effective network congestion at negligible cost (one TCP connection, tiny fixed-size messages).

---

### Likelihood Explanation

The Filter protocol is a standard supported protocol (`SupportProtocols::Filter`). Any peer that negotiates it can send `GetBlockFilterCheckPoints` without any prior authentication or proof-of-work. The message is a fixed 8-byte struct (`start_number: Uint64`), so the attacker's bandwidth cost is essentially zero. The condition `latest >= start_number` is trivially satisfied with `start_number=0` whenever at least one block filter has been built. [7](#0-6) 

---

### Recommendation

Add a keyed `RateLimiter<(PeerIndex, u32)>` to `BlockFilter` (mirroring `Relayer`) and check it at the top of `try_process` before dispatching any inbound request message. The same fix should be applied to `GetBlockFilterHashes` and `GetBlockFilters`, which share the same structural gap. [8](#0-7) 

---

### Proof of Concept

1. Run a CKB node with block filter enabled; wait until filter data is built for at least 1 block.
2. Connect a peer that negotiates `SupportProtocols::Filter`.
3. In a tight loop, send `BlockFilterMessage { GetBlockFilterCheckPoints { start_number: 0 } }` (8-byte payload each).
4. Observe via RocksDB metrics or `perf stat` that each received message triggers ~2,000 `get_block_hash` + ~2,000 `get_block_filter_hash` DB reads.
5. Confirm no rate-limit rejection is ever returned and the node's I/O wait climbs proportionally to message rate. [9](#0-8)

### Citations

**File:** sync/src/filter/get_block_filter_check_points_process.rs (L9-10)
```rust
const BATCH_SIZE: BlockNumber = 2000;
const CHECK_POINT_INTERVAL: BlockNumber = 2000;
```

**File:** sync/src/filter/get_block_filter_check_points_process.rs (L34-69)
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

**File:** sync/src/relayer/mod.rs (L78-99)
```rust
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
    }
```

**File:** sync/src/relayer/mod.rs (L112-123)
```rust
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

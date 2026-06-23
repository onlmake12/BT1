### Title
Missing Per-Peer Rate Limiting on `GetBlockFilterHashes` Enables DB Read Amplification DoS — (`sync/src/filter/mod.rs`)

---

### Summary

The `BlockFilter` protocol handler processes `GetBlockFilterHashes` messages with no per-peer rate limiting. Each message triggers up to 4,000 sequential RocksDB reads (2,000 iterations × `get_block_hash` + `get_block_filter_hash`). A single unprivileged peer can flood this handler at maximum TCP rate, saturating the shared RocksDB read path and degrading block/tx relay for all peers.

---

### Finding Description

`GetBlockFilterHashesProcess::execute` loops up to `BATCH_SIZE = 2000` times, issuing two RocksDB reads per iteration: [1](#0-0) [2](#0-1) 

That is up to **4,000 RocksDB point-reads per message**. The `BlockFilter` protocol handler struct carries no rate-limiter field and performs no rate-limit check before dispatching: [3](#0-2) [4](#0-3) 

Contrast this with the `Relayer` handler, which explicitly holds a `rate_limiter: RateLimiter<(PeerIndex, u32)>` and gates every non-PoW message through it before dispatch: [5](#0-4) [6](#0-5) 

The same pattern (keyed `governor::RateLimiter` per `(PeerIndex, message_type_id)`) is also present in the `HolePunching` handler: [7](#0-6) [8](#0-7) 

The `BlockFilter` handler is the only production protocol handler that omits this guard entirely. The same gap also affects `GetBlockFilters` (1,000-iteration loop) and `GetBlockFilterCheckPoints` (2,000-iteration loop with a 2,000-block stride): [9](#0-8) [10](#0-9) 

---

### Impact Explanation

RocksDB is shared across all CKB subsystems (block relay, tx relay, chain state). Flooding `GetBlockFilterHashes` from a single peer at TCP line rate causes sustained high-IOPS RocksDB reads that compete with block/tx relay I/O, increasing P2P relay latency for all connected peers. The attacker pays only the cost of a TCP connection and sending ~50-byte messages; the victim pays 4,000 RocksDB reads per message.

---

### Likelihood Explanation

The attack requires only a standard P2P connection to a node that has enabled block filter building (opt-in but common for light-client-serving nodes). No PoW, no stake, no privileged access. The missing guard is a straightforward omission relative to the existing pattern in `Relayer` and `HolePunching`.

---

### Recommendation

Add a `rate_limiter: RateLimiter<(PeerIndex, u32)>` field to the `BlockFilter` handler in `sync/src/filter/mod.rs`, mirroring the existing pattern in `Relayer::new` and `Relayer::try_process`. Apply the check at the top of `BlockFilter::try_process` before dispatching to any of the three process handlers. A limit of 30 req/s per peer per message type (matching the Relayer's quota) is a reasonable starting point.

---

### Proof of Concept

1. Connect a single peer to a CKB node with filter data built for a large number of blocks.
2. In a tight loop, send `GetBlockFilterHashes { start_number: 0 }` (a ~10-byte message).
3. Each message causes the node to execute the 2,000-iteration loop in `GetBlockFilterHashesProcess::execute`, issuing ~4,000 RocksDB reads.
4. Monitor RocksDB read IOPS (via metrics or `rocksdb.stats`) and P2P relay latency for other peers — both will degrade proportionally to the flood rate, with no ban or throttle applied to the attacking peer.

### Citations

**File:** sync/src/filter/get_block_filter_hashes_process.rs (L8-8)
```rust
const BATCH_SIZE: BlockNumber = 2000;
```

**File:** sync/src/filter/get_block_filter_hashes_process.rs (L52-66)
```rust
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

**File:** sync/src/filter/get_block_filters_process.rs (L9-9)
```rust
const BATCH_SIZE: BlockNumber = 1000;
```

**File:** sync/src/filter/get_block_filter_check_points_process.rs (L9-10)
```rust
const BATCH_SIZE: BlockNumber = 2000;
const CHECK_POINT_INTERVAL: BlockNumber = 2000;
```

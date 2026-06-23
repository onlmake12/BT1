### Title
Missing Per-Peer Rate Limiter in BlockFilter Protocol Enables Multi-Peer RocksDB Read Amplification - (`sync/src/filter/get_block_filter_check_points_process.rs`)

---

### Summary

The `BlockFilter` P2P protocol handler has no per-peer rate limiter. Any unprivileged connected peer can repeatedly send `GetBlockFilterCheckPoints` messages, each triggering up to 4,000 unbounded RocksDB reads (2,000 × `get_block_hash` + 2,000 × `get_block_filter_hash`). With up to 125 connected peers (default `max_peers`) sending at high frequency, the aggregate DB read cost is unbounded and unthrottled, causing measurable performance degradation on the node.

---

### Finding Description

`GetBlockFilterCheckPointsProcess::execute` iterates up to `BATCH_SIZE=2000` times, each iteration performing two RocksDB reads: [1](#0-0) [2](#0-1) 

The loop increments `block_number` by `CHECK_POINT_INTERVAL=2000` per step, so on a chain with ≥4,000,000 blocks the full 2,000-iteration batch is reachable from a single `start_number=0` message, yielding 4,000 DB reads per call. [3](#0-2) 

The `BlockFilter` handler that dispatches this has **no rate limiter**: [4](#0-3) [5](#0-4) 

By contrast, the `Relayer` protocol explicitly constructs a `rate_limiter` keyed by `(peer, message.item_id())` capped at 30 req/sec and checks it before every dispatch: [6](#0-5) [7](#0-6) 

The `HolePunching` protocol applies the same pattern: [8](#0-7) 

`BlockFilter` is the only message-serving protocol that omits this guard entirely.

---

### Impact Explanation

Default deployment allows up to 125 inbound peers (`max_peers = 125`): [9](#0-8) 

Each peer sending `GetBlockFilterCheckPoints(start_number=0)` at high frequency forces the node to execute up to 4,000 RocksDB reads per message with no throttle. The same pattern applies to `GetBlockFilterHashes` (BATCH_SIZE=2000, increments by 1, so 4,000 reads over 2,000 consecutive blocks) and `GetBlockFilters` (BATCH_SIZE=1000): [10](#0-9) [11](#0-10) 

Aggregate impact: sustained RocksDB read pressure, elevated CPU from cache lookups and deserialization, and potential eviction of hot block-processing data from the 256 MB RocksDB block cache, degrading normal sync and relay throughput.

---

### Likelihood Explanation

The attack requires only a standard P2P connection — no PoW, no keys, no privileged role. The `GetBlockFilterCheckPoints` message is a 9-byte struct (`start_number: Uint64`): [12](#0-11) 

Any peer that negotiates the `Filter` protocol (listed in `support_protocols` by default) can send it at will: [13](#0-12) 

---

### Recommendation

Add a per-peer rate limiter to `BlockFilter` following the existing `Relayer` pattern: construct a `governor::RateLimiter` keyed by `(PeerIndex, message.item_id())` in `BlockFilter::new`, and check it at the top of `try_process` before dispatching to any of the three process handlers. A cap of 1–2 req/sec per peer per message type is sufficient for legitimate light-client use.

---

### Proof of Concept

1. Connect 125 peers to a target node, all negotiating `SupportProtocols::Filter`.
2. Each peer sends `GetBlockFilterCheckPoints { start_number: 0 }` in a tight loop (≥10 Hz).
3. Monitor RocksDB block cache miss rate (`rocksdb.block.cache.miss`) and CPU time attributed to filter reads vs. normal block processing.
4. Observe that RocksDB read throughput scales linearly with peer count and message rate, with no server-side throttle applied, while normal block relay latency increases. [14](#0-13)

### Citations

**File:** sync/src/filter/get_block_filter_check_points_process.rs (L9-10)
```rust
const BATCH_SIZE: BlockNumber = 2000;
const CHECK_POINT_INTERVAL: BlockNumber = 2000;
```

**File:** sync/src/filter/get_block_filter_check_points_process.rs (L34-56)
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
```

**File:** sync/src/filter/mod.rs (L22-25)
```rust
pub struct BlockFilter {
    /// Sync shared state
    shared: Arc<SyncShared>,
}
```

**File:** sync/src/filter/mod.rs (L50-54)
```rust
            packed::BlockFilterMessageUnionReader::GetBlockFilterCheckPoints(msg) => {
                GetBlockFilterCheckPointsProcess::new(msg, self, nc, peer)
                    .execute()
                    .await
            }
```

**File:** sync/src/relayer/mod.rs (L89-99)
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

**File:** resource/ckb.toml (L94-95)
```text
max_peers = 125
max_outbound_peers = 8
```

**File:** resource/ckb.toml (L112-112)
```text
support_protocols = ["Ping", "Discovery", "Identify", "Feeler", "DisconnectMessage", "Sync", "Relay", "Time", "Alert", "LightClient", "Filter", "HolePunching"]
```

**File:** sync/src/filter/get_block_filter_hashes_process.rs (L8-8)
```rust
const BATCH_SIZE: BlockNumber = 2000;
```

**File:** sync/src/filter/get_block_filters_process.rs (L9-9)
```rust
const BATCH_SIZE: BlockNumber = 1000;
```

**File:** util/gen-types/schemas/extensions.mol (L231-233)
```text
struct GetBlockFilterCheckPoints {
    start_number:   Uint64,
}
```

### Title
Missing Per-Peer Rate Limiting in `BlockFilter` Protocol Allows Single Peer to Trigger Unbounded RocksDB Read Flood — (`sync/src/filter/get_block_filter_check_points_process.rs`)

---

### Summary

The `BlockFilter` P2P protocol handler has no per-peer rate limiter. Any unprivileged remote peer can send `GetBlockFilterCheckPoints{start_number: 0}` at maximum network rate. Each message causes up to `BATCH_SIZE=2000` loop iterations, each performing two RocksDB reads (`get_block_hash` + `get_block_filter_hash`), totalling up to **4,000 DB reads per message** with no throttle. The `Relayer` protocol has an explicit 30 req/sec per-(peer, message-type) rate limiter; the `BlockFilter` protocol has none.

---

### Finding Description

`GetBlockFilterCheckPointsProcess::execute` loops up to `BATCH_SIZE` (2000) times, advancing `block_number` by `CHECK_POINT_INTERVAL` (2000) each iteration: [1](#0-0) [2](#0-1) 

Each iteration calls `get_block_hash(block_number)` then `get_block_filter_hash(&block_hash)` — two synchronous RocksDB point-reads per step, up to 4,000 per message.

The `BlockFilter` struct carries only `shared: Arc<SyncShared>` — no rate limiter field: [3](#0-2) 

`try_process` dispatches directly to the handler with zero rate-check: [4](#0-3) 

Contrast with `Relayer`, which carries a `RateLimiter<(PeerIndex, u32)>` and enforces 30 req/sec per (peer, message-type) before any handler runs: [5](#0-4) [6](#0-5) 

A grep for `rate_limit`, `RateLimiter`, `quota`, or `per_second` in `sync/src/filter/**` returns **zero matches**, confirming the omission is complete.

---

### Impact Explanation

On a mainnet node with block filter enabled and ≥1 block of filter data built (the stated precondition), `latest >= 0` is always true, so every `GetBlockFilterCheckPoints{start_number: 0}` message unconditionally enters the 2000-iteration loop. At mainnet height (~14 M blocks), all 2000 iterations complete, issuing 4,000 RocksDB reads per message. A single TCP connection sending these messages at line rate saturates the shared RocksDB read path, degrading block relay and sync for all other peers — matching the stated scope of "CKB network congestion with few costs."

---

### Likelihood Explanation

The attack requires only a standard P2P connection to a node with block filter enabled. No PoW, no keys, no privileged role. The message is 9 bytes (a `struct GetBlockFilterCheckPoints { start_number: Uint64 }`). The attacker can reuse the same connection indefinitely. The `GetBlockFilterHashes` handler has the identical gap (`BATCH_SIZE=2000`, same two DB reads per step, same missing rate limiter), amplifying the attack surface. [7](#0-6) 

---

### Recommendation

Add a `RateLimiter<(PeerIndex, u32)>` field to `BlockFilter` (mirroring `Relayer`) and check it at the top of `try_process` before dispatching to any handler. A quota of 1–2 req/sec per (peer, message-type) is sufficient for legitimate light-client use while eliminating the flood vector.

---

### Proof of Concept

1. Connect to a CKB full node with block filter enabled via the Filter protocol (`SupportProtocols::Filter`).
2. In a tight loop, send `BlockFilterMessage { GetBlockFilterCheckPoints { start_number: 0 } }` (9-byte payload).
3. Observe via `perf stat` or RocksDB statistics that the node's DB read IOPS spike proportionally to message rate, while block relay latency for other peers increases.

The entry path is:

```
P2P receive → BlockFilter::received (mod.rs:122)
           → BlockFilter::process (mod.rs:70)
           → BlockFilter::try_process (mod.rs:33)
           → GetBlockFilterCheckPointsProcess::execute (get_block_filter_check_points_process.rs:34)
           → loop 2000× { get_block_hash + get_block_filter_hash }  ← 4000 DB reads, no rate check
``` [8](#0-7)

### Citations

**File:** sync/src/filter/get_block_filter_check_points_process.rs (L9-10)
```rust
const BATCH_SIZE: BlockNumber = 2000;
const CHECK_POINT_INTERVAL: BlockNumber = 2000;
```

**File:** sync/src/filter/get_block_filter_check_points_process.rs (L43-56)
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
                let Some(next_block_number) = block_number.checked_add(CHECK_POINT_INTERVAL) else {
                    break;
                };
                block_number = next_block_number;
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

**File:** sync/src/filter/mod.rs (L33-54)
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

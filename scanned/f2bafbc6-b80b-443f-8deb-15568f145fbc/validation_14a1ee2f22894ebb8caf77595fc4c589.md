Audit Report

## Title
Missing Per-Peer Rate Limiting in `BlockFilter` Protocol Allows Unbounded RocksDB Read Flood — (`sync/src/filter/mod.rs`, `sync/src/filter/get_block_filter_check_points_process.rs`)

## Summary
The `BlockFilter` P2P protocol handler contains no per-peer rate limiter. Any remote peer can send `GetBlockFilterCheckPoints{start_number: 0}` at maximum network rate, triggering up to 4,000 synchronous RocksDB reads per message (2,000 loop iterations × 2 reads each). The `Relayer` protocol enforces an explicit 30 req/sec per-(peer, message-type) rate limit; `BlockFilter` has none, leaving the shared RocksDB read path fully exposed.

## Finding Description
`GetBlockFilterCheckPointsProcess::execute` iterates up to `BATCH_SIZE = 2000` times, advancing `block_number` by `CHECK_POINT_INTERVAL = 2000` per step. [1](#0-0) 

Each iteration performs two synchronous RocksDB point-reads: `get_block_hash(block_number)` followed by `get_block_filter_hash(&block_hash)`. [2](#0-1) 

The `BlockFilter` struct carries only `shared: Arc<SyncShared>` — no rate limiter field exists. [3](#0-2) 

`try_process` dispatches directly to the handler with zero rate-check before any handler runs. [4](#0-3) 

By contrast, `Relayer` carries a `RateLimiter<(PeerIndex, u32)>` initialized at 30 req/sec per (peer, message-type): [5](#0-4) 

And enforces it at the top of `try_process` before any handler executes: [6](#0-5) 

`GetBlockFilterHashesProcess` has the identical gap: `BATCH_SIZE = 2000`, same two DB reads per step, same missing rate limiter, doubling the attack surface. [7](#0-6) 

The full call path is:
```
P2P receive → BlockFilter::received (mod.rs:122)
           → BlockFilter::process (mod.rs:70)
           → BlockFilter::try_process (mod.rs:33)  ← no rate check
           → GetBlockFilterCheckPointsProcess::execute
           → loop 2000× { get_block_hash + get_block_filter_hash }  ← 4000 DB reads
```

## Impact Explanation
A single TCP connection sending `GetBlockFilterCheckPoints{start_number: 0}` at line rate saturates the shared RocksDB read path. At mainnet height (~14M blocks), all 2,000 iterations complete unconditionally (since `latest >= 0` is always true), issuing 4,000 RocksDB reads per message. This degrades block relay and sync for all other peers. This matches the allowed High impact: **"Vulnerabilities or bad designs which could cause CKB network congestion with few costs."**

## Likelihood Explanation
The attack requires only a standard P2P connection to a node with block filter enabled. No PoW, no keys, no privileged role is needed. The message payload is 9 bytes (`GetBlockFilterCheckPoints { start_number: Uint64 }`). The attacker can reuse the same connection indefinitely. The identical gap in `GetBlockFilterHashesProcess` provides a second independent flood vector from the same connection.

## Recommendation
Add a `RateLimiter<(PeerIndex, u32)>` field to `BlockFilter` (mirroring `Relayer`) and check it at the top of `try_process` before dispatching to any handler. A quota of 1–2 req/sec per (peer, message-type) is sufficient for legitimate light-client use while eliminating the flood vector. The `governor` crate is already a dependency (used by `Relayer`), so no new dependency is required.

## Proof of Concept
1. Connect to a CKB full node with block filter enabled via `SupportProtocols::Filter`.
2. In a tight loop, send `BlockFilterMessage { GetBlockFilterCheckPoints { start_number: 0 } }` (9-byte payload).
3. Observe via RocksDB statistics (`rocksdb.block.cache.miss`, `rocksdb.number.db.get`) that DB read IOPS spike proportionally to message rate, while block relay latency for other peers increases.
4. Repeat with `GetBlockFilterHashes { start_number: 0 }` to confirm the identical gap in the second handler.

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

Audit Report

## Title
Missing Per-Peer Rate Limit on `GetBlockFilterCheckPoints` Enables DB Read Amplification — (`sync/src/filter/get_block_filter_check_points_process.rs`)

## Summary
The `BlockFilter` protocol handler contains no rate limiter. Any peer that negotiates `SupportProtocols::Filter` can flood the node with `GetBlockFilterCheckPoints(start_number=0)` messages, each unconditionally triggering up to 4,000 synchronous RocksDB reads (2,000 × `get_block_hash` + 2,000 × `get_block_filter_hash`) with no throttling. This saturates the node's storage I/O at negligible attacker cost, degrading sync, relay, and block-propagation processing.

## Finding Description
`GetBlockFilterCheckPointsProcess::execute` iterates up to `BATCH_SIZE = 2000` times, performing two DB reads per iteration: [1](#0-0) [2](#0-1) 

The only guard is `if latest >= start_number`, which is trivially satisfied with `start_number=0` whenever any filter data exists. There is no per-peer throttle before or after this check.

The `BlockFilter` struct carries only `shared: Arc<SyncShared>` — no `rate_limiter` field exists: [3](#0-2) 

`try_process` dispatches directly to `GetBlockFilterCheckPointsProcess::execute` with no rate-limit check: [4](#0-3) 

This is in direct contrast to `Relayer`, which holds a keyed `RateLimiter<(PeerIndex, u32)>` and gates every non-PoW message through it before dispatch: [5](#0-4) [6](#0-5) 

The same structural gap exists in `GetBlockFilterHashes` and `GetBlockFilters`, which are dispatched through the same unguarded `try_process`.

## Impact Explanation
This matches the allowed CKB bounty impact: **"Vulnerabilities or bad designs which could cause CKB network congestion with few costs" — High (10001–15000 points)**. A single attacker with one TCP connection and 8-byte messages can continuously saturate the node's disk I/O, degrading its ability to process sync, relay, and block-propagation messages — causing effective network-level degradation at negligible cost.

## Likelihood Explanation
The Filter protocol is a standard supported protocol. Any peer that negotiates it can send `GetBlockFilterCheckPoints` without authentication or proof-of-work. The message is a fixed 8-byte struct (`start_number: Uint64`), so attacker bandwidth cost is essentially zero. The condition `latest >= start_number` is trivially satisfied with `start_number=0` whenever at least one block filter has been built.

## Recommendation
Add a keyed `RateLimiter<(PeerIndex, u32)>` to `BlockFilter` (mirroring `Relayer`) and check it at the top of `try_process` before dispatching any inbound request message. The same fix should be applied to `GetBlockFilterHashes` and `GetBlockFilters`, which share the same structural gap.

## Proof of Concept
1. Run a CKB node with block filter enabled; wait until filter data is built for at least 1 block.
2. Connect a peer that negotiates `SupportProtocols::Filter`.
3. In a tight loop, send `BlockFilterMessage { GetBlockFilterCheckPoints { start_number: 0 } }` (8-byte payload each).
4. Observe via RocksDB metrics or `perf stat` that each received message triggers ~2,000 `get_block_hash` + ~2,000 `get_block_filter_hash` DB reads.
5. Confirm no rate-limit rejection is ever returned and the node's I/O wait climbs proportionally to message rate.

### Citations

**File:** sync/src/filter/get_block_filter_check_points_process.rs (L9-10)
```rust
const BATCH_SIZE: BlockNumber = 2000;
const CHECK_POINT_INTERVAL: BlockNumber = 2000;
```

**File:** sync/src/filter/get_block_filter_check_points_process.rs (L41-56)
```rust
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

**File:** sync/src/relayer/mod.rs (L78-82)
```rust
pub struct Relayer {
    chain: ChainController,
    pub(crate) shared: Arc<SyncShared>,
    rate_limiter: RateLimiter<(PeerIndex, u32)>,
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

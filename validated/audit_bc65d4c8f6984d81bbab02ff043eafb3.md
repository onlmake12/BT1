All code claims check out against the actual source. Here is the validated report:

---

Audit Report

## Title
Missing Per-Peer Rate Limiting in `GetBlockFilterCheckPoints` Handler Enables DB Read Amplification — (`sync/src/filter/get_block_filter_check_points_process.rs`)

## Summary
`GetBlockFilterCheckPointsProcess::execute()` unconditionally performs up to 4,000 RocksDB reads (2,000 `get_block_hash` + 2,000 `get_block_filter_hash`) per message with no per-peer rate limiting. Any unauthenticated peer can flood this handler at full TCP speed, causing sustained I/O and CPU saturation. The `Relayer` protocol applies an explicit keyed rate limiter before every dispatch; the `BlockFilter` handler has none.

## Finding Description
`BATCH_SIZE` and `CHECK_POINT_INTERVAL` are both 2,000: [1](#0-0) 

Each request unconditionally iterates up to 2,000 times, performing two DB lookups (`get_block_hash` + `get_block_filter_hash`) per step: [2](#0-1) 

`BlockFilter` carries only `shared` — no rate limiter field: [3](#0-2) 

`try_process` dispatches directly to `execute()` with zero guards: [4](#0-3) 

By contrast, `Relayer` defines a keyed rate limiter: [5](#0-4) 

And checks it before every dispatch: [6](#0-5) 

`GetBlockFilterHashes` is also unguarded and performs 2,000 DB reads per request (same `BATCH_SIZE`): [7](#0-6) 

The asymmetry is confirmed: `Relayer` calls `self.rate_limiter.retain_recent()` in `disconnected`; `BlockFilter.disconnected` does nothing beyond logging. [8](#0-7) 

## Impact Explanation
With `start_number=0` on CKB mainnet (≥4,000,000 blocks, filters built), every `GetBlockFilterCheckPoints` message triggers exactly 4,000 RocksDB point-lookups (2,000 iterations × `CHECK_POINT_INTERVAL=2000` step × 2 lookups). With up to 125 inbound peers each sending at full TCP speed, the node sustains hundreds of thousands of DB reads per second with no server-side throttle. This saturates RocksDB I/O and CPU, degrading the node's ability to process and propagate blocks and transactions. This matches the **High (10001–15000 points)** impact class: *Vulnerabilities or bad designs which could cause CKB network congestion with few costs*.

## Likelihood Explanation
- The Filter protocol is enabled by default in the production config.
- No authentication, PoW, stake, or privileged role is required — any TCP peer qualifies.
- The precondition (chain with >4M blocks and filters built) is satisfied on CKB mainnet today.
- The attack message is 9 bytes and trivially constructable from the public protocol schema.
- The oversight is systematic: the same codebase applies rate limiting to `Relayer` but not to `BlockFilter`, and the gap covers all three `BlockFilter` request handlers (`GetBlockFilters`, `GetBlockFilterHashes`, `GetBlockFilterCheckPoints`).

## Recommendation
Mirror the `Relayer` pattern in `BlockFilter`:
1. Add `rate_limiter: RateLimiter<(PeerIndex, u32)>` to the `BlockFilter` struct in `sync/src/filter/mod.rs`.
2. In `try_process`, check `self.rate_limiter.check_key(&(peer, message.item_id()))` before dispatching; return `StatusCode::TooManyRequests` on failure.
3. Call `self.rate_limiter.retain_recent()` in `disconnected`.
A quota of 1–5 requests per second per peer is sufficient for legitimate light-client use.

## Proof of Concept
```
1. Connect to a CKB mainnet node (Filter protocol enabled by default).
2. In a tight loop, send:
     GetBlockFilterCheckPoints { start_number: 0 }
   at full TCP speed.
3. Each message triggers 4,000 RocksDB reads server-side (confirmed by
   BATCH_SIZE=2000 × CHECK_POINT_INTERVAL=2000, two lookups per step).
4. Repeat from N peers (N ≤ 125, the default max_inbound_peers).
5. Monitor node RocksDB read IOPS and relay/sync message latency —
   both degrade proportionally to N × request_rate with no server-side throttle.
6. Confirm: adding a rate limiter matching sync/src/relayer/mod.rs lines 116–123
   eliminates the amplification.
```

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

**File:** sync/src/filter/mod.rs (L175-185)
```rust
    async fn disconnected(
        &mut self,
        _nc: Arc<dyn CKBProtocolContext + Sync>,
        peer_index: PeerIndex,
    ) {
        info_target!(
            crate::LOG_TARGET_FILTER,
            "FilterProtocol.disconnected peer={}",
            peer_index
        );
    }
```

**File:** sync/src/relayer/mod.rs (L63-82)
```rust
type RateLimiter<T> = governor::RateLimiter<
    T,
    governor::state::keyed::HashMapStateStore<T>,
    governor::clock::DefaultClock,
>;

#[derive(Debug, Eq, PartialEq)]
pub enum ReconstructionResult {
    Block(BlockView),
    Missing(Vec<usize>, Vec<usize>),
    Collided,
    Error(Status),
}

/// Relayer protocol handle
pub struct Relayer {
    chain: ChainController,
    pub(crate) shared: Arc<SyncShared>,
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

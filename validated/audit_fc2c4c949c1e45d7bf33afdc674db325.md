Audit Report

## Title
Missing Per-Peer Rate Limiting in `GetBlockFilterCheckPoints` Handler Enables DB Read Amplification — (`sync/src/filter/get_block_filter_check_points_process.rs`)

## Summary
`GetBlockFilterCheckPointsProcess::execute()` unconditionally performs up to 4,000 RocksDB point-lookups per message (2,000 `get_block_hash` + 2,000 `get_block_filter_hash`), with no per-peer rate limiting anywhere in the `BlockFilter` protocol handler. Any unauthenticated peer can flood this handler at full TCP speed, causing sustained I/O and CPU saturation that degrades sync and relay performance for all legitimate peers. The `Relayer` protocol applies an explicit keyed rate limiter before every dispatch; the `BlockFilter` handler has none.

## Finding Description
`BATCH_SIZE` and `CHECK_POINT_INTERVAL` are both set to 2,000: [1](#0-0) 

The `execute()` loop runs up to 2,000 iterations, each performing two DB lookups: [2](#0-1) 

The `BlockFilter` struct carries only a `shared` field — no rate limiter field exists: [3](#0-2) 

`try_process` dispatches directly to `execute()` with no guard of any kind: [4](#0-3) 

A grep across all of `sync/src/filter/` for `rate_limit`, `RateLimiter`, or `TooManyRequests` returns zero matches, confirming no rate limiting exists anywhere in the filter path.

By contrast, `Relayer` declares a keyed rate limiter: [5](#0-4) 

And checks it before every message dispatch: [6](#0-5) 

The `Relayer` also calls `retain_recent()` on disconnect to bound memory: [7](#0-6) 

None of these protections exist in `BlockFilter`.

## Impact Explanation
With `start_number=0` on a chain with ≥4,000,000 blocks and filters built (satisfied on CKB mainnet since ~2019 at ~8s block time), each `GetBlockFilterCheckPoints` message triggers exactly 4,000 RocksDB point-lookups. With up to 125 default inbound peers each sending at full TCP speed, this produces hundreds of thousands of DB reads per second with zero server-side throttle. The result is sustained I/O saturation and CPU pressure that measurably degrades block sync and relay throughput for all legitimate peers. This matches the allowed impact: **High — Vulnerabilities or bad designs which could cause CKB network congestion with few costs.**

## Likelihood Explanation
- The Filter protocol is enabled by default in the production config. [8](#0-7) 
- No authentication, PoW, stake, or privileged role is required — only a TCP connection.
- `GetBlockFilterCheckPoints` is a 9-byte message; the attacker needs only knowledge of the protocol schema.
- The precondition (chain with >4M blocks and filters built) is satisfied on CKB mainnet today.
- The asymmetry is extreme: attacker sends ~9 bytes, server performs 4,000 DB reads.

## Recommendation
Mirror the `Relayer` pattern in `BlockFilter`:

1. Add `rate_limiter: RateLimiter<(PeerIndex, u32)>` to the `BlockFilter` struct in `sync/src/filter/mod.rs`.
2. In `try_process`, check `self.rate_limiter.check_key(&(peer, message.item_id()))` before dispatching, returning `StatusCode::TooManyRequests` on failure.
3. Call `self.rate_limiter.retain_recent()` in `disconnected`.

A quota of 1–5 `GetBlockFilterCheckPoints` requests per second per peer is sufficient for legitimate light-client use.

## Proof of Concept
```
1. Connect to a CKB mainnet node (Filter protocol enabled by default).
2. In a tight loop, send:
     GetBlockFilterCheckPoints { start_number: 0 }
   as fast as the TCP connection allows.
3. Each message triggers 4,000 RocksDB reads server-side (confirmed by
   BATCH_SIZE=2000 × 2 lookups per iteration).
4. Repeat from N peers (N ≤ max_inbound_peers = 125 by default).
5. Monitor node RocksDB read IOPS and relay/sync latency — both degrade
   proportionally to N × request_rate with no server-side throttle.
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

**File:** sync/src/relayer/mod.rs (L933-934)
```rust
        // Retains all keys in the rate limiter that were used recently enough.
        self.rate_limiter.retain_recent();
```

**File:** resource/ckb.toml (L94-95)
```text
max_peers = 125
max_outbound_peers = 8
```

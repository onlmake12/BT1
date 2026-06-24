Audit Report

## Title
Unbounded Per-Message DB Read Loop with No Rate Limiting in `GetBlockFilterHashesProcess::execute` Enables Resource Exhaustion — (`sync/src/filter/get_block_filter_hashes_process.rs`)

## Summary

`GetBlockFilterHashesProcess::execute` loops up to `BATCH_SIZE = 2000` times, performing two RocksDB reads per iteration (up to 4,000 reads per message). The filter protocol handler dispatches directly to this function with no per-peer rate limiter, while the relay protocol has an explicit keyed rate limiter capped at 30 requests/second per peer per message type. A remote peer with a standard TCP connection can flood the node with `GetBlockFilterHashes(start_number=0)` messages at negligible cost, forcing unbounded DB read work and degrading sync throughput for all peers.

## Finding Description

`BATCH_SIZE` is set to 2000 at [1](#0-0) 

The guard `latest >= start_number` is trivially satisfied when `start_number=0` because `latest` is a `u64` and is always `>= 0`, so the full loop always executes on a node with any built filter blocks: [2](#0-1) 

Each loop iteration calls `get_block_hash` then `get_block_filter_hash` — two synchronous RocksDB reads — up to 2,000 times per message: [3](#0-2) 

`BlockFilter::try_process` dispatches directly to `GetBlockFilterHashesProcess::execute` with no rate check: [4](#0-3) 

By contrast, `Relayer` carries a `RateLimiter<(PeerIndex, u32)>` keyed by peer and message type, initialized at 30 req/sec, and checks it before every dispatch: [5](#0-4) [6](#0-5) 

A grep for `rate_limit`, `RateLimiter`, `throttle`, and `quota` across all of `sync/src/**/*.rs` returns zero matches in any filter file, confirming the absence of any rate control in the filter protocol. [7](#0-6) 

## Impact Explanation

This is a **High** severity finding matching the allowed impact: *"Vulnerabilities or bad designs which could cause CKB network congestion with few costs."* An attacker sending `GetBlockFilterHashes(start_number=0)` in a tight loop forces up to 4,000 synchronous RocksDB reads per message at negligible attacker cost (8-byte messages over a standard TCP connection). Because the async handler processes messages sequentially per protocol instance, this saturates RocksDB read I/O and starves the async executor, degrading sync throughput for all connected peers on the targeted node. Targeting multiple nodes simultaneously degrades network-wide sync performance with minimal attacker resources — a classic low-cost amplification pattern.

## Likelihood Explanation

No special privileges, PoW, stake, or key material are required. The Filter protocol is enabled by default. The attacker needs only a standard TCP connection. The message payload is 8 bytes. The cost-to-impact ratio is highly asymmetric: each message forces 4,000 DB reads on the victim while costing the attacker essentially nothing. The attack is trivially repeatable and scriptable.

## Recommendation

1. Add a per-peer, per-message-type rate limiter to `BlockFilter::try_process`, mirroring the `governor`-based `RateLimiter<(PeerIndex, u32)>` already present in `Relayer::try_process` with a comparable quota (e.g., 30 req/sec).
2. Optionally enforce a minimum `start_number` advancement between successive requests from the same peer to prevent identical repeated queries.
3. Consider whether `BATCH_SIZE = 2000` is necessary or can be reduced to limit worst-case work per message.

## Proof of Concept

1. Spin up a CKB node with ≥2000 blocks and block filters built (Filter protocol enabled by default).
2. Connect a custom peer and send `GetBlockFilterHashes { start_number: 0 }` in a tight loop (e.g., 100 messages/second).
3. Monitor RocksDB read IOPS via metrics — they will spike at ~400,000 reads/second per attacker connection.
4. Connect a legitimate sync peer concurrently and measure its sync throughput — it degrades proportionally as the executor is occupied serving the flood.
5. Confirm no ban or rate-limit response is returned by the node for any of the flood messages.

### Citations

**File:** sync/src/filter/get_block_filter_hashes_process.rs (L8-8)
```rust
const BATCH_SIZE: BlockNumber = 2000;
```

**File:** sync/src/filter/get_block_filter_hashes_process.rs (L39-39)
```rust
        if latest >= start_number {
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

**File:** sync/src/filter/mod.rs (L1-18)
```rust
mod get_block_filter_check_points_process;
mod get_block_filter_hashes_process;
mod get_block_filters_process;

use crate::{Status, types::SyncShared};
use get_block_filter_check_points_process::GetBlockFilterCheckPointsProcess;
use get_block_filter_hashes_process::GetBlockFilterHashesProcess;
use get_block_filters_process::GetBlockFiltersProcess;

use crate::utils::{MetricDirection, metric_ckb_message_bytes};
use ckb_constant::sync::BAD_MESSAGE_BAN_TIME;
use ckb_logger::{debug_target, error_target, info_target, warn_target};
use ckb_network::{
    CKBProtocolContext, CKBProtocolHandler, PeerIndex, SupportProtocols, async_trait, bytes::Bytes,
};
use ckb_types::{packed, prelude::*};
use std::sync::Arc;
use std::time::Instant;
```

**File:** sync/src/filter/mod.rs (L45-49)
```rust
            packed::BlockFilterMessageUnionReader::GetBlockFilterHashes(msg) => {
                GetBlockFilterHashesProcess::new(msg, self, nc, peer)
                    .execute()
                    .await
            }
```

**File:** sync/src/relayer/mod.rs (L81-81)
```rust
    rate_limiter: RateLimiter<(PeerIndex, u32)>,
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

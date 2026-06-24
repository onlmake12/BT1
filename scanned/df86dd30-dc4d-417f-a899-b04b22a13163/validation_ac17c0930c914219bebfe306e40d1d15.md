Audit Report

## Title
Missing Per-Peer Rate Limiting in `BlockFilter::received` Enables Unbounded RocksDB Read Amplification — (`sync/src/filter/mod.rs`, `sync/src/filter/get_block_filter_hashes_process.rs`)

## Summary

The `BlockFilter` P2P protocol handler contains no rate limiter. Any peer can send an unlimited stream of `GetBlockFilterHashes` messages, each triggering up to 4,002 RocksDB point lookups on the victim node (2 for the parent hash + up to 4,000 in the batch loop), while the attacker's cost per message is a fixed 8-byte payload. This creates a severe and unbounded I/O amplification that can degrade or halt a CKB node's participation in the network.

## Finding Description

**Root cause:** `BlockFilter` has no `rate_limiter` field and `BlockFilter::received` calls `self.process(...)` unconditionally with no rate check.

`BlockFilter` struct definition: [1](#0-0) 

`received` handler — no rate check before `process`: [2](#0-1) 

`GetBlockFilterHashesProcess::execute` — parent hash lookup (2 DB reads): [3](#0-2) 

Batch loop — up to `BATCH_SIZE = 2000` iterations × 2 DB reads = up to 4,000 additional reads: [4](#0-3) [5](#0-4) 

**Contrast with `Relayer`**, which has an explicit `rate_limiter` field and checks it before any processing: [6](#0-5) [7](#0-6) 

**Existing guards are insufficient:** The only guard in `received` is a malformed-message ban. A well-formed `GetBlockFilterHashes` message (a single `Uint64`, 8 bytes) passes this check unconditionally and proceeds to full DB processing with no throttle. [8](#0-7) 

## Impact Explanation

Each 8-byte attacker message forces up to 4,002 RocksDB point lookups. By cycling `start_number` across non-overlapping 2,000-block windows (0, 2000, 4000, …), the attacker maximizes RocksDB block-cache misses and drives disk I/O. At modest message rates (e.g., 1,000 msg/s over a single TCP connection), the victim node sustains ~4 million DB reads/second, saturating its I/O subsystem and starving the async runtime of capacity for legitimate Sync and Relay message processing. This matches the allowed bounty impact: **High — Vulnerabilities or bad designs which could cause CKB network congestion with few costs**, and potentially **High — Vulnerabilities which could easily crash a CKB node**.

## Likelihood Explanation

The attack requires only a single valid P2P connection to a node with `SupportProtocols::Filter` enabled. No authentication, no PoW, no stake. The message is a well-formed protocol message that passes all existing validation. The attacker can sustain the flood indefinitely from a single low-bandwidth connection (~8 KB/s for 1,000 msg/s). The victim has no mechanism to detect or throttle the flood.

## Recommendation

Mirror the `Relayer` pattern:

1. Add `rate_limiter: RateLimiter<(PeerIndex, u32)>` to `BlockFilter` in `sync/src/filter/mod.rs`.
2. Initialize it in `BlockFilter::new` with a quota of 1–5 requests/second per peer per message type (sufficient for legitimate light-client use).
3. In `BlockFilter::received` (or `try_process`), check `self.rate_limiter.check_key(&(peer_index, msg.item_id()))` before calling `self.process(...)`, returning early (and optionally banning the peer) on excess.

## Proof of Concept

```
1. Connect to a CKB node with SupportProtocols::Filter enabled.
2. In a tight loop, send GetBlockFilterHashes messages with start_number
   cycling through 0, 2000, 4000, ..., (tip - 2000) to maximize cache misses.
3. Each message is 8 bytes + framing overhead (~tens of bytes total).
4. Observe on the victim:
   - RocksDB read amplification (rocksdb.block.cache.miss metric spikes)
   - CPU saturation in the async runtime
   - Degraded response times / timeouts for Sync/Relay protocol messages
5. 1,000 messages → ~4,000,000 DB reads; attacker bandwidth: ~8 KB total.
```

### Citations

**File:** sync/src/filter/mod.rs (L22-25)
```rust
pub struct BlockFilter {
    /// Sync shared state
    shared: Arc<SyncShared>,
}
```

**File:** sync/src/filter/mod.rs (L128-143)
```rust
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
```

**File:** sync/src/filter/mod.rs (L151-152)
```rust
        let start_time = Instant::now();
        self.process(nc, peer_index, msg).await;
```

**File:** sync/src/filter/get_block_filter_hashes_process.rs (L8-8)
```rust
const BATCH_SIZE: BlockNumber = 2000;
```

**File:** sync/src/filter/get_block_filter_hashes_process.rs (L40-50)
```rust
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

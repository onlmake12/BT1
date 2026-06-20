The code evidence is conclusive. Here is the analysis:

---

**Key facts from the code:**

**`BlockFilter` struct has no rate limiter field:** [1](#0-0) 

Compare to `Relayer`, which has an explicit `rate_limiter: RateLimiter<(PeerIndex, u32)>` field: [2](#0-1) 

And `HolePunching`, which checks rate limits in `received()` before any processing: [3](#0-2) 

**`BlockFilter::received` dispatches directly to `process()` with no rate check:** [4](#0-3) 

**`GetBlockFilterHashesProcess::execute` performs up to 2000 sequential DB lookups per request:** [5](#0-4) 

**When `start_number=0`, the `parent_block_filter_hash` branch returns `Byte32::zero()` (no DB lookup needed to short-circuit), so the full 2000-iteration loop always runs:** [6](#0-5) 

---

### Title
Missing Per-Peer Rate Limit on Filter Protocol Allows Amplified DB Read and Bandwidth Exhaustion — (`sync/src/filter/mod.rs`, `sync/src/filter/get_block_filter_hashes_process.rs`)

### Summary

The `BlockFilter` protocol handler has no rate limiter, unlike `Relayer` and `HolePunching`. Any unprivileged remote peer can send `GetBlockFilterHashes(start_number=0)` at arbitrary frequency. Each message causes up to 2000 sequential `get_block_hash` + `get_block_filter_hash` DB reads and a ~64 KB response, with no throttle, no ban, and no quota.

### Finding Description

`BlockFilter` in `sync/src/filter/mod.rs` holds only `shared: Arc<SyncShared>` — there is no `rate_limiter` field. The `received()` method parses the message and immediately calls `self.process()`, which calls `try_process()`, which dispatches to `GetBlockFilterHashesProcess::execute()`.

Inside `execute()`, when `latest >= start_number` (trivially satisfied with `start_number=0` and any built filter data), the code loops up to `BATCH_SIZE = 2000` times, calling `active_chain.get_block_hash(block_number)` and `active_chain.get_block_filter_hash(&block_hash)` on each iteration — 2 DB reads per block, 4000 DB reads total per request. It then serializes and sends a `BlockFilterHashes` response containing up to 2000 × 32 bytes = 64 KB.

By contrast:
- `Relayer::try_process()` checks `self.rate_limiter.check_key(&(peer, message.item_id()))` and returns `StatusCode::TooManyRequests` before any work.
- `HolePunching::received()` checks `self.rate_limiter.check_key(...)` before dispatching.
- `BlockFilter` has neither.

The same absence applies to `GetBlockFilters` and `GetBlockFilterCheckPoints` handlers in the same module.

### Impact Explanation

A single attacker connection sending 1000 `GetBlockFilterHashes(start_number=0)` messages per second causes:
- ~4,000,000 DB reads/second on the victim node
- ~64 MB/second of outbound bandwidth consumed toward the attacker
- No ban is issued; no `TooManyRequests` status is returned; the node processes every message

This degrades DB throughput for all other node operations (block sync, tx relay) and can saturate outbound bandwidth, congesting the node's network with very few attacker connections.

### Likelihood Explanation

The attack requires only a valid P2P connection to a node with block filter enabled (`ckb.block_filter_enable = true`). No authentication, no PoW, no special privilege. The attacker controls `start_number` (a single `u64` field) and can send messages at line rate. The condition `latest >= 0` is always true once any filter is built.

### Recommendation

Add a `RateLimiter<(PeerIndex, u32)>` field to `BlockFilter` (matching the pattern in `Relayer::new()`) and check it at the top of `try_process()` before dispatching to any of the three request handlers. A quota of 30 requests/second per peer (matching the Relayer's cap) would be a consistent baseline.

### Proof of Concept

```
1. Connect to a CKB node with block_filter_enable=true and ≥1 built filter block.
2. In a loop, send packed::GetBlockFilterHashes { start_number: 0 } at maximum rate.
3. Observe: node returns a full BlockFilterHashes response for every message.
4. Assert: no TooManyRequests status, no ban_peer call, DB read counter increases
   by ~4000 per message, outbound bytes increase by ~64 KB per message.
```

### Citations

**File:** sync/src/filter/mod.rs (L21-25)
```rust
#[derive(Clone)]
pub struct BlockFilter {
    /// Sync shared state
    shared: Arc<SyncShared>,
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

**File:** sync/src/filter/get_block_filter_hashes_process.rs (L39-50)
```rust
        if latest >= start_number {
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

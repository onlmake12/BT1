Based on my investigation of the code, here is my assessment:

---

### Title
Missing Per-Peer Rate Limiter on Filter Protocol Enables Resource Exhaustion DoS — (`sync/src/filter/mod.rs`, `sync/src/filter/get_block_filters_process.rs`)

### Summary

The `BlockFilter` P2P protocol handler has no per-peer rate limiter, unlike `Relayer` which explicitly enforces one. Any unprivileged remote peer can flood a node with `GetBlockFilters` messages, each triggering up to 1000 DB reads and up to 1.8 MB of response allocation, with no throttling applied.

### Finding Description

**Confirmed: `BlockFilter` has no rate limiter.**

The `BlockFilter` struct in `sync/src/filter/mod.rs` contains only:

```rust
pub struct BlockFilter {
    shared: Arc<SyncShared>,
}
``` [1](#0-0) 

By contrast, `Relayer` in `sync/src/relayer/mod.rs` explicitly holds a `rate_limiter: RateLimiter<(PeerIndex, u32)>` and checks it at the top of `try_process` before dispatching any message (30 req/sec hard cap per peer+message-type key): [2](#0-1) [3](#0-2) 

`HolePunching` similarly has `rate_limiter` and `forward_rate_limiter` fields: [4](#0-3) 

**Confirmed: Each `GetBlockFilters` message triggers up to 1000 DB reads and up to 1.8 MB allocation.**

`GetBlockFiltersProcess::execute` loops up to `BATCH_SIZE = 1000` times, calling `get_block_hash` + `get_block_filter` (two DB reads per iteration), accumulating data until the 1.8 MB cap is hit: [5](#0-4) [6](#0-5) 

The 1.8 MB cap was added specifically to avoid tentacle frame-size disconnects, not as a DoS mitigation: [7](#0-6) 

The same absence of rate limiting applies to `GetBlockFilterHashes` (`BATCH_SIZE=2000`) and `GetBlockFilterCheckPoints` (`BATCH_SIZE=2000`): [8](#0-7) [9](#0-8) 

**The `received` → `process` → `try_process` → `execute` call chain has no guard:** [10](#0-9) 

### Impact Explanation

An attacker opens connections to a CKB full node (up to `max_peers`, typically 125) and floods each with `GetBlockFilters(start_number=0)` at maximum rate. Each message causes:
- Up to 1000 synchronous RocksDB reads (2000 for hashes/checkpoints variants)
- Up to 1.8 MB of heap allocation for the response

With N concurrent peers each sending at maximum rate, the node's async runtime is saturated with DB-bound work, starving legitimate sync/relay processing. This degrades or stalls the node. Across the network, all full nodes serving the Filter protocol are equally vulnerable, enabling a low-cost, network-wide DoS.

### Likelihood Explanation

The attack requires only a standard P2P connection to the Filter protocol — no PoW, no keys, no privileged access. The message is 8 bytes on the wire (`start_number` only). The asymmetry between attacker cost (8 bytes sent) and victim cost (up to 1000 DB reads + 1.8 MB response) is extreme. The missing rate limiter is a clear oversight given that `Relayer` and `HolePunching` both have explicit per-peer rate limiters for exactly this reason.

### Recommendation

Add a `rate_limiter: RateLimiter<(PeerIndex, u32)>` field to `BlockFilter` (mirroring `Relayer`'s pattern) and check it at the top of `try_process` before dispatching to any of the three process handlers. A quota of ~5–10 requests/second per peer per message type would be sufficient for legitimate light-client use while eliminating the flood vector.

### Proof of Concept

1. Build a CKB node with block filter enabled (`--block-filter-enable`).
2. Mine enough blocks so filter data exists.
3. Open `max_peers` connections to the node's Filter protocol port.
4. From each connection, send `GetBlockFilters { start_number: 0 }` in a tight loop.
5. Observe: node CPU and RocksDB I/O saturate; sync/relay message latency degrades; node becomes unresponsive to legitimate peers.

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

**File:** network/src/protocols/hole_punching/mod.rs (L38-47)
```rust
pub(crate) struct HolePunching {
    network_state: Arc<NetworkState>,
    bind_addr: Option<SocketAddr>,
    // Request timestamp recorded
    inflight_requests: HashMap<PeerId, u64>,
    // Delivered timestamp recorded
    pending_delivered: HashMap<PeerId, PendingDeliveredInfo>,
    rate_limiter: RateLimiter<(PeerIndex, u32)>,
    forward_rate_limiter: RateLimiter<(PeerId, PeerId, u32)>,
}
```

**File:** sync/src/filter/get_block_filters_process.rs (L9-9)
```rust
const BATCH_SIZE: BlockNumber = 1000;
```

**File:** sync/src/filter/get_block_filters_process.rs (L45-57)
```rust
            for _ in 0..BATCH_SIZE {
                if let Some(block_hash) = active_chain.get_block_hash(block_number) {
                    if let Some(block_filter) = active_chain.get_block_filter(&block_hash) {
                        if current_content_size
                            + block_hash.as_slice().len()
                            + 4
                            + block_filter.as_slice().len()
                            + 4
                            >= (1.8 * 1024.0 * 1024.0) as usize
                        {
                            // Break if the encoded size of `block_hash` + `block_filter` + `start_number` + molecule header increase reaches 1.8MB, to avoid frame size too large
                            break;
                        }
```

**File:** sync/src/filter/get_block_filter_hashes_process.rs (L8-8)
```rust
const BATCH_SIZE: BlockNumber = 2000;
```

**File:** sync/src/filter/get_block_filter_check_points_process.rs (L9-10)
```rust
const BATCH_SIZE: BlockNumber = 2000;
const CHECK_POINT_INTERVAL: BlockNumber = 2000;
```

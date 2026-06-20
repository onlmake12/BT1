The code evidence is conclusive. Let me confirm the key structural comparison between protocols.

### Title
Unbounded Per-Peer Request Rate in `GetBlockFiltersProcess::execute` Enables Sustained CPU/I/O Exhaustion — (`sync/src/filter/get_block_filters_process.rs`)

---

### Summary

The `BlockFilter` protocol handler has no per-peer rate limiter. Any remote peer can send `GetBlockFilters` messages at an unlimited rate, each causing up to 1,000 RocksDB reads and serialization of up to 1.8 MB of filter data, with no throttling or backpressure applied.

---

### Finding Description

`GetBlockFiltersProcess::execute` iterates up to `BATCH_SIZE = 1000` blocks per request, performing two RocksDB lookups per block (`get_block_hash` + `get_block_filter`) and accumulating up to 1.8 MB of response data before serializing and sending it. [1](#0-0) [2](#0-1) 

The `BlockFilter` protocol handler's `received` method dispatches directly to `self.process()` with no rate check: [3](#0-2) 

The `BlockFilter` struct itself carries only `shared: Arc<SyncShared>` — there is no `rate_limiter` field: [4](#0-3) 

This is in direct contrast to the `Relayer` protocol, which holds a `rate_limiter: RateLimiter<(PeerIndex, u32)>` and gates every incoming message through it before dispatch: [5](#0-4) [6](#0-5) 

The `HolePunching` protocol similarly has both a `rate_limiter` and a `forward_rate_limiter`, checked in `received` before any processing: [7](#0-6) 

A `grep` for `rate_limiter` across all of `sync/src/` confirms it appears **only** in `sync/src/relayer/mod.rs` — the `BlockFilter` handler has none.

---

### Impact Explanation

Each `GetBlockFilters{start_number: 0}` message causes the victim node to:
- Execute up to 1,000 RocksDB point-reads (two per block: hash + filter data)
- Allocate and serialize up to ~1.8 MB of response data
- Transmit that response back to the requesting peer

A single attacker peer sending this message in a tight loop can sustain continuous I/O pressure on RocksDB and saturate the async task processing the Filter protocol, degrading overall node performance and potentially causing network congestion for legitimate peers.

---

### Likelihood Explanation

The Filter protocol is a production feature enabled for light-client support. Any peer that speaks the Filter protocol can connect and immediately begin flooding requests. No PoW, no stake, no privileged role is required. The exploit requires only a TCP connection and knowledge of the `GetBlockFilters` message format, which is public.

---

### Recommendation

Add a per-peer, per-message-type rate limiter to `BlockFilter`, mirroring the pattern already used in `Relayer`:

1. Add a `rate_limiter: RateLimiter<(PeerIndex, u32)>` field to `BlockFilter`.
2. In `BlockFilter::received` (or `try_process`), check `self.rate_limiter.check_key(&(peer_index, msg.item_id()))` and return early (or return `StatusCode::TooManyRequests`) if the limit is exceeded.
3. Call `self.rate_limiter.retain_recent()` in `disconnected`.

A quota of 1–5 requests per second per peer per message type is sufficient for legitimate light-client use.

---

### Proof of Concept

```
1. Connect to a CKB full node that has block filters built (e.g., 1000+ blocks).
2. Negotiate the Filter protocol (SupportProtocols::Filter).
3. In a tight loop, send:
     GetBlockFilters { start_number: 0 }
   encoded as a BlockFilterMessage molecule frame.
4. Observe on the victim node:
   - RocksDB read IOPS spike to maximum
   - CPU usage on the filter-protocol async task increases proportionally
   - Legitimate light-client peers experience delayed or dropped responses
```

The `BATCH_SIZE = 1000` constant and the 1.8 MB cap are the only bounds on per-request work; there is no bound on request frequency. [1](#0-0) [4](#0-3) [8](#0-7)

### Citations

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

**File:** sync/src/relayer/mod.rs (L63-99)
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

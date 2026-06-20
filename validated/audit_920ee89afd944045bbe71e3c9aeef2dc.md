### Title
Unbounded DB Read Amplification via Unrate-Limited Filter Protocol Handlers — (`sync/src/filter/mod.rs`, `sync/src/filter/get_block_filters_process.rs`, `sync/src/filter/get_block_filter_hashes_process.rs`, `sync/src/filter/get_block_filter_check_points_process.rs`)

---

### Summary

The `BlockFilter` protocol handler has **no rate limiter** of any kind. Any connected peer can send `GetBlockFilters`, `GetBlockFilterHashes`, and `GetBlockFilterCheckPoints` messages in an unbounded tight loop, triggering up to ~10,000 RocksDB reads per message-triple with zero throttling. This is a concrete, verifiable gap relative to the `Relayer` and `HolePunching` protocols, which both carry explicit per-peer, per-message-type rate limiters.

---

### Finding Description

**The missing guard — structural comparison:**

`Relayer` carries a rate limiter field and checks it before every handler dispatch: [1](#0-0) 

```rust
pub struct Relayer {
    chain: ChainController,
    pub(crate) shared: Arc<SyncShared>,
    rate_limiter: RateLimiter<(PeerIndex, u32)>,  // ← present
}
``` [2](#0-1) 

`HolePunching` similarly carries two rate limiters and checks them in `received()`: [3](#0-2) [4](#0-3) 

`BlockFilter`, by contrast, has only:

```rust
pub struct BlockFilter {
    shared: Arc<SyncShared>,   // ← no rate_limiter field
}
``` [5](#0-4) 

Its `received()` handler goes directly from message parsing to `self.process()` with no rate check: [6](#0-5) 

A grep for `rate_limiter` across all of `sync/src/**` returns matches only in `sync/src/relayer/mod.rs` — zero hits in any filter file.

**Per-message DB read cost:**

| Message | BATCH_SIZE | Reads/iteration | Max DB reads |
|---|---|---|---|
| `GetBlockFilters` | 1 000 | `get_block_hash` + `get_block_filter` = 2 | ~2 000 |
| `GetBlockFilterHashes` | 2 000 | `get_block_hash` + `get_block_filter_hash` = 2 | ~4 000 |
| `GetBlockFilterCheckPoints` | 2 000 | `get_block_hash` + `get_block_filter_hash` = 2 | ~4 000 | [7](#0-6) [8](#0-7) [9](#0-8) [10](#0-9) [11](#0-10) [12](#0-11) 

All three reads go to `ChainDB` (RocksDB) via `ActiveChain`: [13](#0-12) 

**The `try_process` dispatcher** routes all three message types directly to their handlers with no interposed guard: [14](#0-13) 

---

### Impact Explanation

A single connected peer sending all three message types in a tight loop can sustain ~10,000 RocksDB `get()` calls per message-triple, bounded only by network throughput and the async executor's scheduling. On a node with a large chain and built filters, this saturates I/O bandwidth and competes with block processing, sync, and transaction relay — degrading or stalling normal node operation. The `GetBlockFilters` handler also has a 1.8 MB response size cap that limits bandwidth amplification but does not reduce the number of DB reads triggered. [15](#0-14) 

---

### Likelihood Explanation

The attacker path requires only a standard P2P connection — no PoW, no key, no privileged role. The node must have block filters built (i.e., `block_filter` feature enabled and chain synced), which is the intended production deployment for light-client support. The attack is repeatable, requires no state, and is trivially scriptable.

---

### Recommendation

Add a `RateLimiter<(PeerIndex, u32)>` field to `BlockFilter` (mirroring `Relayer`'s pattern) and check it in `try_process()` before dispatching to any of the three handlers. A quota of 30 req/sec per (peer, message-type) pair — consistent with `Relayer` — would bound the per-peer DB read rate to at most ~120,000 reads/sec across all three types combined, which is a manageable ceiling.

---

### Proof of Concept

```
1. Connect to a synced CKB node with block filters enabled (Filter protocol).
2. In a tight loop, send:
   a. GetBlockFilters { start_number: 0 }
   b. GetBlockFilterHashes { start_number: 0 }
   c. GetBlockFilterCheckPoints { start_number: 0 }
3. Monitor RocksDB statistics (rocksdb.block.cache.miss, rocksdb.get.hit.l0, etc.)
   or use perf/strace to count syscall I/O.
4. Assert: aggregate get() calls per second are unbounded and scale linearly
   with message send rate, with no TooManyRequests rejection observed.
5. Compare against the same test on the Relay protocol, which returns
   StatusCode::TooManyRequests after 30 messages/sec per type.
```

### Citations

**File:** sync/src/relayer/mod.rs (L81-92)
```rust
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

**File:** network/src/protocols/hole_punching/mod.rs (L45-47)
```rust
    rate_limiter: RateLimiter<(PeerIndex, u32)>,
    forward_rate_limiter: RateLimiter<(PeerId, PeerId, u32)>,
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

**File:** sync/src/filter/mod.rs (L21-25)
```rust
#[derive(Clone)]
pub struct BlockFilter {
    /// Sync shared state
    shared: Arc<SyncShared>,
}
```

**File:** sync/src/filter/mod.rs (L33-68)
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
            packed::BlockFilterMessageUnionReader::BlockFilters(_)
            | packed::BlockFilterMessageUnionReader::BlockFilterHashes(_)
            | packed::BlockFilterMessageUnionReader::BlockFilterCheckPoints(_) => {
                // remote peer should not send block filter to us without asking
                // TODO: ban remote peer
                warn_target!(
                    crate::LOG_TARGET_FILTER,
                    "Received unexpected message from peer: {:?}",
                    peer
                );
                Status::ignored()
            }
        }
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

**File:** sync/src/filter/get_block_filters_process.rs (L9-9)
```rust
const BATCH_SIZE: BlockNumber = 1000;
```

**File:** sync/src/filter/get_block_filters_process.rs (L45-47)
```rust
            for _ in 0..BATCH_SIZE {
                if let Some(block_hash) = active_chain.get_block_hash(block_number) {
                    if let Some(block_filter) = active_chain.get_block_filter(&block_hash) {
```

**File:** sync/src/filter/get_block_filters_process.rs (L48-56)
```rust
                        if current_content_size
                            + block_hash.as_slice().len()
                            + 4
                            + block_filter.as_slice().len()
                            + 4
                            >= (1.8 * 1024.0 * 1024.0) as usize
                        {
                            // Break if the encoded size of `block_hash` + `block_filter` + `start_number` + molecule header increase reaches 1.8MB, to avoid frame size too large
                            break;
```

**File:** sync/src/filter/get_block_filter_hashes_process.rs (L8-8)
```rust
const BATCH_SIZE: BlockNumber = 2000;
```

**File:** sync/src/filter/get_block_filter_hashes_process.rs (L53-56)
```rust
            for _ in 0..BATCH_SIZE {
                if let Some(block_filter_hash) = active_chain
                    .get_block_hash(block_number)
                    .and_then(|block_hash| active_chain.get_block_filter_hash(&block_hash))
```

**File:** sync/src/filter/get_block_filter_check_points_process.rs (L9-10)
```rust
const BATCH_SIZE: BlockNumber = 2000;
const CHECK_POINT_INTERVAL: BlockNumber = 2000;
```

**File:** sync/src/filter/get_block_filter_check_points_process.rs (L43-46)
```rust
            for _ in 0..BATCH_SIZE {
                if let Some(block_filter_hash) = active_chain
                    .get_block_hash(block_number)
                    .and_then(|block_hash| active_chain.get_block_filter_hash(&block_hash))
```

**File:** sync/src/types/mod.rs (L1664-1670)
```rust
    pub fn get_block_filter(&self, hash: &packed::Byte32) -> Option<packed::Bytes> {
        self.store().get_block_filter(hash)
    }

    pub fn get_block_filter_hash(&self, hash: &packed::Byte32) -> Option<packed::Byte32> {
        self.store().get_block_filter_hash(hash)
    }
```

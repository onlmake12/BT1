### Title
Missing Per-Peer Rate Limiting in BlockFilter Protocol Allows RocksDB I/O Saturation — (`sync/src/filter/mod.rs`, `sync/src/filter/get_block_filters_process.rs`)

### Summary

The `BlockFilter` protocol handler processes every incoming `GetBlockFilters` message with no per-peer rate limiting. Each such message triggers up to 1000 `get_block_hash` + 1000 `get_block_filter` RocksDB reads. An unprivileged remote peer can flood the serving node at maximum TCP rate, saturating RocksDB I/O and starving block validation and sync threads. The `Relayer` protocol has an explicit 30 req/s `RateLimiter` keyed by `(PeerIndex, message_type)` for exactly this reason; `BlockFilter` has no equivalent.

---

### Finding Description

**Entry point:** Any peer that connects on the `Filter` protocol (no authentication required).

**Trigger condition:** The target node must have at least one built block filter, i.e., `get_latest_built_filter_block_number() >= 0`. This is the normal operating state of any synced node with filters enabled.

**Per-message work:** `GetBlockFiltersProcess::execute` loops up to `BATCH_SIZE = 1000` iterations, calling `active_chain.get_block_hash(block_number)` and `active_chain.get_block_filter(&block_hash)` on each iteration — both are RocksDB point reads. [1](#0-0) [2](#0-1) 

The only guard before entering the loop is `if latest >= start_number`. With `start_number=0` and any built filter present, this is always true. [3](#0-2) 

**No rate limiting in `BlockFilter`:** The `BlockFilter` struct has no `rate_limiter` field, and `BlockFilter::received` dispatches directly to `try_process` without any throttle check. [4](#0-3) [5](#0-4) 

**Contrast with `Relayer`:** The `Relayer` struct carries a `RateLimiter<(PeerIndex, u32)>` initialized at 30 req/s per `(peer, message_type)` pair, and `try_process` checks it before dispatching any message. [6](#0-5) [7](#0-6) [8](#0-7) 

`GetBlockFilterHashesProcess` has the same issue with `BATCH_SIZE = 2000`. [9](#0-8) 

---

### Impact Explanation

Each `GetBlockFilters(start_number=0)` message causes up to 2000 synchronous RocksDB reads (1000 hash lookups + 1000 filter data reads). At even modest TCP rates (e.g., 100 msg/s per peer), a single attacker connection generates 200,000 RocksDB reads/s. Multiple connections multiply this linearly. RocksDB's read IOPS budget is shared with block validation and chain sync; saturation directly degrades block processing latency and can stall the node's tip advancement.

---

### Likelihood Explanation

- Requires only a standard P2P connection on the Filter protocol — no credentials, no PoW, no stake.
- The precondition (at least one built filter) is the default state of any production node with filters enabled.
- The attack is trivially scriptable: open N connections, loop `send(GetBlockFilters{start_number: 0})`.

---

### Recommendation

Add a `RateLimiter<(PeerIndex, u32)>` field to `BlockFilter` (mirroring `Relayer`) and check it at the top of `BlockFilter::try_process` before dispatching `GetBlockFilters`, `GetBlockFilterHashes`, and `GetBlockFilterCheckPoints`. A quota of 10–30 req/s per `(peer, message_type)` is consistent with the existing Relayer policy. [10](#0-9) 

---

### Proof of Concept

```
1. Connect N peers to the target node's Filter protocol endpoint.
2. Each peer sends in a tight loop:
       GetBlockFilters { start_number: 0 }
3. Observe via RocksDB metrics or `perf` that get_block_hash /
   get_block_filter read IOPS scale linearly with N.
4. Observe that block validation latency (time from compact block
   receipt to BLOCK_STORED) degrades proportionally to N.
5. Confirm that adding a 30 req/s RateLimiter (as in Relayer) caps
   IOPS regardless of N.
```

### Citations

**File:** sync/src/filter/get_block_filters_process.rs (L9-9)
```rust
const BATCH_SIZE: BlockNumber = 1000;
```

**File:** sync/src/filter/get_block_filters_process.rs (L38-47)
```rust
        if latest >= start_number {
            let mut block_hashes = Vec::new();
            let mut filters = Vec::new();
            let mut current_content_size = 0;
            current_content_size += 8; // Size of start_number
            current_content_size += 4 * 2; // Size of the header field `full-size` of `block_hash` and `block_filter`
            let mut block_number = start_number;
            for _ in 0..BATCH_SIZE {
                if let Some(block_hash) = active_chain.get_block_hash(block_number) {
                    if let Some(block_filter) = active_chain.get_block_filter(&block_hash) {
```

**File:** sync/src/filter/mod.rs (L22-25)
```rust
pub struct BlockFilter {
    /// Sync shared state
    shared: Arc<SyncShared>,
}
```

**File:** sync/src/filter/mod.rs (L122-153)
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
```

**File:** sync/src/relayer/mod.rs (L63-98)
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

**File:** sync/src/filter/get_block_filter_hashes_process.rs (L8-8)
```rust
const BATCH_SIZE: BlockNumber = 2000;
```

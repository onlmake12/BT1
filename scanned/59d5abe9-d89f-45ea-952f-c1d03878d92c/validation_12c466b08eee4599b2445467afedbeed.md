The code is clear. Let me confirm the key comparison between the `Relayer` (which has rate limiting) and `BlockFilter` (which does not).

### Title
Missing Per-Peer Rate Limit on `GetBlockFilters` Enables Unbounded RocksDB Read Amplification — (`sync/src/filter/get_block_filters_process.rs`)

---

### Summary

The `BlockFilter` P2P protocol handler has no per-peer rate limiter. Any unprivileged remote peer can send `GetBlockFilters` messages in a tight loop, causing the node to execute up to 2,000 RocksDB reads per message (1,000 `get_block_hash` + 1,000 `get_block_filter`) with zero backpressure. The only existing guard — a 1.8 MB response-size cap — limits response payload size but does nothing to throttle request rate or aggregate I/O cost per peer per time window.

---

### Finding Description

`GetBlockFiltersProcess::execute` iterates up to `BATCH_SIZE = 1000` times, issuing two RocksDB lookups per iteration: [1](#0-0) [2](#0-1) 

The `BlockFilter` struct holds only `shared: Arc<SyncShared>` — there is no `rate_limiter` field, and `try_process` dispatches directly to `execute()` with no rate check: [3](#0-2) [4](#0-3) 

Compare this to the `Relayer` protocol, which explicitly carries a `governor::RateLimiter` keyed by `(PeerIndex, message_type)` and enforces a 30 req/sec cap before any processing: [5](#0-4) [6](#0-5) 

The 1.8 MB size guard added in v0.203.0 only breaks the inner loop when the *response payload* would exceed the frame limit — it does not prevent the attacker from immediately sending the next `GetBlockFilters(start_number=0)` message: [7](#0-6) 

The same issue applies to `GetBlockFilterHashes` (`BATCH_SIZE = 2000`) and `GetBlockFilterCheckPoints` (`BATCH_SIZE = 2000`) in the same protocol handler, which are also unguarded. [8](#0-7) [9](#0-8) 

---

### Impact Explanation

A single attacker peer sending `GetBlockFilters(start_number=0)` at maximum network speed forces the target node into a continuous loop of up to 2,000 RocksDB point-reads per message. RocksDB I/O saturation degrades the node's ability to serve all other peers (block sync, relay, honest light clients), causing effective network-level congestion at negligible attacker cost (each request message is 12 bytes). Multiple attacker peers multiply the effect linearly.

---

### Likelihood Explanation

The attack requires only a valid P2P connection to a node that has built filter data for at least one block (the `latest >= start_number` guard at line 38 is trivially satisfied with `start_number=0`). No PoW, no stake, no privileged role. The `Filter` protocol is a standard supported protocol (`SupportProtocols::Filter`). The attack is mechanically identical to the pattern the `Relayer` rate limiter was introduced to prevent. [10](#0-9) 

---

### Recommendation

Add a `governor::RateLimiter<(PeerIndex, u32)>` field to `BlockFilter` (mirroring `Relayer`) and check it at the top of `try_process` before dispatching to any of the three `Get*` handlers. A quota of 30 req/sec per peer per message type (matching the Relayer's cap) would bound worst-case DB reads to 30 × 2,000 = 60,000 reads/sec per peer — still high but manageable and consistent with existing policy. [11](#0-10) [12](#0-11) 

---

### Proof of Concept

```
1. Connect a single peer to a CKB full node with block filter enabled (>= 1 block built).
2. In a tight loop, send:
     BlockFilterMessage { GetBlockFilters { start_number: 0 } }
   at maximum TCP throughput (message is 12 bytes; easily thousands per second).
3. Monitor the target node's RocksDB read IOPS (e.g., via /metrics or iostat).
4. Assert: read IOPS scale linearly with message send rate, with no plateau or backpressure.
5. Observe: legitimate peers experience sync stalls / timeouts as I/O is saturated.
```

### Citations

**File:** sync/src/filter/get_block_filters_process.rs (L9-9)
```rust
const BATCH_SIZE: BlockNumber = 1000;
```

**File:** sync/src/filter/get_block_filters_process.rs (L38-38)
```rust
        if latest >= start_number {
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

**File:** sync/src/filter/mod.rs (L22-25)
```rust
pub struct BlockFilter {
    /// Sync shared state
    shared: Arc<SyncShared>,
}
```

**File:** sync/src/filter/mod.rs (L27-31)
```rust
impl BlockFilter {
    /// Create a new block filter protocol handler
    pub fn new(shared: Arc<SyncShared>) -> Self {
        Self { shared }
    }
```

**File:** sync/src/filter/mod.rs (L39-44)
```rust
        match message {
            packed::BlockFilterMessageUnionReader::GetBlockFilters(msg) => {
                GetBlockFiltersProcess::new(msg, self, nc, peer)
                    .execute()
                    .await
            }
```

**File:** sync/src/relayer/mod.rs (L81-81)
```rust
    rate_limiter: RateLimiter<(PeerIndex, u32)>,
```

**File:** sync/src/relayer/mod.rs (L88-98)
```rust
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

**File:** sync/src/filter/get_block_filter_hashes_process.rs (L8-8)
```rust
const BATCH_SIZE: BlockNumber = 2000;
```

**File:** sync/src/filter/get_block_filter_check_points_process.rs (L9-9)
```rust
const BATCH_SIZE: BlockNumber = 2000;
```

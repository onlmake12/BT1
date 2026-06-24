All claims are verified against the actual code. The report is accurate:

- `BATCH_SIZE = 1000` confirmed at [1](#0-0) 
- The loop performs two RocksDB reads per iteration with no rate guard [2](#0-1) 
- `BlockFilter` struct has no rate limiter field [3](#0-2) 
- `try_process` dispatches directly to `execute()` with no preceding check [4](#0-3) 
- `Relayer` has the `rate_limiter` field and enforces it before any dispatch [5](#0-4) 

---

Audit Report

## Title
Missing Per-Peer Rate Limiting on `GetBlockFilters` Enables Resource Exhaustion DoS — (File: `sync/src/filter/get_block_filters_process.rs`)

## Summary
The `BlockFilter` protocol handler dispatches `GetBlockFilters` messages directly to `GetBlockFiltersProcess::execute()` with no per-peer rate check. Each request triggers up to 1,000 sequential RocksDB reads and accumulates up to ~1.8 MB of heap-allocated response data. The `Relayer` protocol enforces a 30 req/s per-peer rate limit via `governor::RateLimiter`; no equivalent guard exists in the filter protocol path.

## Finding Description
In `sync/src/filter/get_block_filters_process.rs`, `BATCH_SIZE` is set to 1,000 (line 9). `execute()` iterates up to that many times (lines 45–72), calling `active_chain.get_block_hash(block_number)` and `active_chain.get_block_filter(&block_hash)` per iteration — two RocksDB reads per block — accumulating results into `block_hashes` and `filters` Vecs until the 1.8 MB cap is reached, then serializing and sending the full response via `async_send_message_to`.

The `BlockFilter` struct in `sync/src/filter/mod.rs` (lines 22–25) holds only `shared: Arc<SyncShared>` with no rate limiter field. Its `try_process` method (lines 39–44) matches `GetBlockFilters` and immediately calls `GetBlockFiltersProcess::new(...).execute().await` with no preceding rate check.

By contrast, `sync/src/relayer/mod.rs` defines `type RateLimiter<T>` (lines 63–67), stores `rate_limiter: RateLimiter<(PeerIndex, u32)>` in the `Relayer` struct (line 81), and in `try_process` checks `self.rate_limiter.check_key(&(peer, message.item_id()))` (lines 116–123) before any dispatch, returning `StatusCode::TooManyRequests` on violation. No such guard exists anywhere in `sync/src/filter/`.

## Impact Explanation
A single malicious peer sending `GetBlockFilters(start_number=0)` in a tight loop forces the node to perform up to 1,000 RocksDB reads and allocate up to ~1.8 MB per request, with no server-side throttle. Multiple concurrent peers amplify this linearly. The result is I/O saturation, async task queue exhaustion, and memory pressure, leading to node unresponsiveness or OOM. This matches the **High** impact class: *Vulnerabilities or bad designs which could cause CKB network congestion with few costs*, and *Vulnerabilities which could easily crash a CKB node*.

## Likelihood Explanation
The attack requires only a valid P2P connection and the ability to send a well-formed `GetBlockFilters` message — a single `Uint64` field, trivially constructable. No authentication, PoW, stake, or special privilege is required. The precondition (node has filters built for a long chain) is the normal production state of any full node with `block_filter` enabled. The attack is repeatable, cheap, and amplifiable with multiple peers.

## Recommendation
Add a `RateLimiter<(PeerIndex, u32)>` field to the `BlockFilter` struct, mirroring the pattern in `Relayer`. In `try_process`, before dispatching to any `*Process::execute()`, call `self.rate_limiter.check_key(&(peer, message.item_id()))` and return `StatusCode::TooManyRequests` on failure. Optionally apply a peer ban after repeated violations. Apply the same guard to `GetBlockFilterHashes` and `GetBlockFilterCheckPoints` handlers in the same `try_process` match block.

## Proof of Concept
```rust
// Attacker: connect N peers, each sending GetBlockFilters(0) in a tight loop
let msg = packed::BlockFilterMessage::new_builder()
    .set(packed::GetBlockFilters::new_builder()
        .start_number(0u64.pack())
        .build())
    .build();
loop {
    net.send(&node, SupportProtocols::Filter, msg.as_bytes());
    // No sleep — no server-side rate limit will stop this.
    // Each iteration: up to 1,000 RocksDB reads + ~1.8 MB allocation on the server.
}
// With N concurrent peers, disk I/O and async task queue are saturated.
```
Verification: instrument `GetBlockFiltersProcess::execute()` with a counter; confirm it fires unboundedly per peer per second with no rejection. Compare with `Relayer::try_process` which returns `TooManyRequests` after 30 req/s.

### Citations

**File:** sync/src/filter/get_block_filters_process.rs (L9-9)
```rust
const BATCH_SIZE: BlockNumber = 1000;
```

**File:** sync/src/filter/get_block_filters_process.rs (L45-67)
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
                        current_content_size +=
                            block_hash.as_slice().len() + block_filter.as_slice().len() + 4;
                        block_hashes.push(block_hash);
                        filters.push(block_filter);
                    } else {
                        break;
                    }
                } else {
                    break;
                }
```

**File:** sync/src/filter/mod.rs (L22-25)
```rust
pub struct BlockFilter {
    /// Sync shared state
    shared: Arc<SyncShared>,
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

**File:** sync/src/relayer/mod.rs (L63-123)
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

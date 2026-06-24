Audit Report

## Title
Unbounded DB Read Amplification via Unauthenticated `GetBlockFilters` Messages with No Per-Peer Rate Limiting — (`sync/src/filter/get_block_filters_process.rs`)

## Summary
The Filter protocol handler processes `GetBlockFilters`, `GetBlockFilterHashes`, and `GetBlockFilterCheckPoints` messages with no per-peer rate limiting. Each `GetBlockFilters` message triggers up to 2,000 synchronous RocksDB reads (1,000 × `get_block_hash` + 1,000 × `get_block_filter`) per message. Any unauthenticated peer can send these messages at maximum rate, saturating the node's I/O subsystem and degrading block propagation and relay for all honest peers.

## Finding Description

**Root cause:** `GetBlockFiltersProcess::execute` loops up to `BATCH_SIZE = 1000` times, performing two DB reads per iteration.

In `sync/src/filter/get_block_filters_process.rs`: [1](#0-0) [2](#0-1) 

The 1.8MB size guard at L48–56 only breaks the loop **after** both `get_block_hash` and `get_block_filter` have already been executed for that iteration — it limits response size, not DB read count: [3](#0-2) 

**No rate limiting in the dispatch path:** `BlockFilter::received` parses the message and calls `self.process(...)` directly with zero throttling: [4](#0-3) 

A grep for `rate_limiter`, `governor`, `RateLimiter`, or `per_second` in `sync/src/filter/` returns zero matches, confirming no throttle exists anywhere in the Filter protocol stack.

**Contrast with Relayer:** The Relay protocol explicitly constructs a governor-based limiter keyed by `(PeerIndex, message_type)` at 30 req/s and enforces it before dispatch: [5](#0-4) [6](#0-5) 

**Same pattern in sibling handlers:**
- `GetBlockFilterHashesProcess`: `BATCH_SIZE = 2000`, 2 DB reads/iteration: [7](#0-6) [8](#0-7) 
- `GetBlockFilterCheckPointsProcess`: `BATCH_SIZE = 2000`, 2 DB reads/iteration: [9](#0-8) [10](#0-9) 

## Impact Explanation

Each `GetBlockFilters(start_number=0)` message causes up to 2,000 synchronous RocksDB reads. With N peers each sending at maximum rate, the node's I/O subsystem and async executor thread pool are saturated proportionally to N × message_rate × 2,000. This degrades block propagation, sync, and relay processing for all honest peers. This matches the **High** impact category: *"Vulnerabilities or bad designs which could cause CKB network congestion with few costs."*

## Likelihood Explanation

The attack requires only a standard P2P connection — no PoW, no stake, no privilege. The Filter protocol is a production protocol enabled on full nodes to serve light clients. The attacker controls `start_number` freely (it is a u64 field validated only by `latest >= start_number`). The attack is locally reproducible with a minimal test harness and is repeatable indefinitely.

## Recommendation

Apply a governor-based per-peer rate limiter to the `BlockFilter` handler, mirroring the existing pattern in `Relayer::new`:

```rust
// In BlockFilter struct
rate_limiter: RateLimiter<(PeerIndex, u32)>,

// In try_process, before dispatching:
if self.rate_limiter.check_key(&(peer, message.item_id())).is_err() {
    return StatusCode::TooManyRequests.with_context(message.item_name());
}
```

A quota of 1–2 req/s per peer per message type is sufficient for legitimate light-client use. Additionally, move the 1.8MB size check to occur before the second DB read (`get_block_filter`) to avoid paying the read cost for entries that will be discarded.

## Proof of Concept

1. Connect N peers to a full node with the Filter protocol enabled.
2. Each peer sends `GetBlockFilters { start_number: 0 }` in a tight loop.
3. Each message triggers up to 1,000 calls to `active_chain.get_block_hash` and 1,000 calls to `active_chain.get_block_filter` — all DB reads, no PoW or auth required.
4. Monitor node CPU, RocksDB I/O, and block relay latency; observe saturation proportional to N × rate.
5. Compare against a patched node with a 2 req/s per-peer rate limiter — saturation disappears.

### Citations

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

**File:** sync/src/relayer/mod.rs (L89-92)
```rust
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

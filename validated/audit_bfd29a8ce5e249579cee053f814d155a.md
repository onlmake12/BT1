Audit Report

## Title
Missing Per-Peer Rate Limiting on `GetBlockFilters` Enables Resource Exhaustion DoS — (`sync/src/filter/get_block_filters_process.rs`)

## Summary
The `BlockFilter` protocol handler dispatches `GetBlockFilters` messages directly to `GetBlockFiltersProcess::execute()` with no per-peer rate check. Each request triggers up to 1,000 sequential RocksDB reads and accumulates up to ~1.8 MB of heap-allocated response data. The `Relayer` protocol, handling analogous request-response patterns, explicitly enforces a 30 req/s per-peer rate limit via `governor::RateLimiter`; no equivalent guard exists in the filter protocol path.

## Finding Description
In `sync/src/filter/get_block_filters_process.rs`, `BATCH_SIZE` is set to 1,000 and `execute()` iterates up to that many times, calling `active_chain.get_block_hash(block_number)` and `active_chain.get_block_filter(&block_hash)` per iteration — two RocksDB reads per block — accumulating results into `block_hashes` and `filters` Vecs until the 1.8 MB cap is reached, then serializing and sending the full response via `async_send_message_to`.

The `BlockFilter` struct in `sync/src/filter/mod.rs` holds only `shared: Arc<SyncShared>` with no rate limiter field. Its `try_process` method matches `GetBlockFilters` and immediately calls `GetBlockFiltersProcess::new(...).execute().await` with no preceding rate check.

By contrast, `sync/src/relayer/mod.rs` defines `type RateLimiter<T> = governor::RateLimiter<T, HashMapStateStore<T>, DefaultClock>`, stores `rate_limiter: RateLimiter<(PeerIndex, u32)>` in the `Relayer` struct, and in `try_process` checks `self.rate_limiter.check_key(&(peer, message.item_id()))` before any dispatch, returning `StatusCode::TooManyRequests` on violation. No such guard exists anywhere in `sync/src/filter/`.

## Impact Explanation
A single malicious peer sending `GetBlockFilters(start_number=0)` in a tight loop forces the node to perform up to 1,000 RocksDB reads and allocate up to ~1.8 MB per request, with no server-side throttle. Multiple concurrent peers amplify this linearly. The result is I/O saturation, async task queue exhaustion, and memory pressure, leading to node unresponsiveness or OOM — matching the **High** impact class: *Vulnerabilities or bad designs which could cause CKB network congestion with few costs*, and potentially *Vulnerabilities which could easily crash a CKB node*.

## Likelihood Explanation
The attack requires only a valid P2P connection and the ability to send a well-formed `GetBlockFilters` message — a single `Uint64` field, trivially constructable. No authentication, PoW, stake, or special privilege is required. The precondition (node has filters built for a long chain) is the normal production state of any full node with `block_filter` enabled. The attack is repeatable, cheap, and amplifiable with multiple peers.

## Recommendation
Add a `RateLimiter<(PeerIndex, u32)>` field to the `BlockFilter` struct, mirroring the pattern in `Relayer`. In `try_process`, before dispatching to any `*Process::execute()`, call `self.rate_limiter.check_key(&(peer, message.item_id()))` and return `StatusCode::TooManyRequests` on failure. Optionally apply a peer ban after repeated violations. Also apply the same guard to `GetBlockFilterHashes` and `GetBlockFilterCheckPoints` handlers in the same `try_process` match block.

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
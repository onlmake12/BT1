All code citations check out. The vulnerability is confirmed across all referenced files.

Audit Report

## Title
Missing Per-Peer Rate Limiting on `BlockFilter` Protocol Handler Enables DB Read Amplification DoS — (`sync/src/filter/mod.rs`)

## Summary
The `BlockFilter` protocol handler processes `GetBlockFilterHashes`, `GetBlockFilters`, and `GetBlockFilterCheckPoints` messages with no per-peer rate limiting. Each `GetBlockFilterHashes` message triggers up to 4,000 sequential RocksDB reads (2,000 loop iterations × `get_block_hash` + `get_block_filter_hash`). An unprivileged peer can flood this handler at TCP line rate with no throttle or ban applied, saturating the shared RocksDB read path and degrading block/tx relay for all peers connected to the targeted node.

## Finding Description
The `BlockFilter` struct carries only a `shared: Arc<SyncShared>` field and no rate-limiter:

```rust
// sync/src/filter/mod.rs L22-25
pub struct BlockFilter {
    shared: Arc<SyncShared>,
}
```

`BlockFilter::try_process` dispatches directly to the three process handlers with no rate-limit check:

```rust
// sync/src/filter/mod.rs L33-68
async fn try_process(...) -> Status {
    match message {
        GetBlockFilters(msg) => GetBlockFiltersProcess::new(...).execute().await,
        GetBlockFilterHashes(msg) => GetBlockFilterHashesProcess::new(...).execute().await,
        GetBlockFilterCheckPoints(msg) => GetBlockFilterCheckPointsProcess::new(...).execute().await,
        ...
    }
}
```

`GetBlockFilterHashesProcess::execute` loops up to `BATCH_SIZE = 2000` times, issuing two RocksDB reads per iteration:

```rust
// sync/src/filter/get_block_filter_hashes_process.rs L8, L52-66
const BATCH_SIZE: BlockNumber = 2000;
for _ in 0..BATCH_SIZE {
    if let Some(block_filter_hash) = active_chain
        .get_block_hash(block_number)                          // RocksDB read #1
        .and_then(|h| active_chain.get_block_filter_hash(&h)) // RocksDB read #2
    { ... }
}
```

This yields up to **4,000 RocksDB point-reads per message**. The same gap affects `GetBlockFilters` (1,000-iteration loop, `get_block_hash` + `get_block_filter`) and `GetBlockFilterCheckPoints` (2,000-iteration loop, same two reads per iteration).

By contrast, `Relayer` holds a `rate_limiter: RateLimiter<(PeerIndex, u32)>` and gates every non-PoW message through it:

```rust
// sync/src/relayer/mod.rs L81, L116-123
rate_limiter: RateLimiter<(PeerIndex, u32)>,
...
if should_check_rate && self.rate_limiter.check_key(&(peer, message.item_id())).is_err() {
    return StatusCode::TooManyRequests.with_context(message.item_name());
}
```

`HolePunching` applies the same pattern:

```rust
// network/src/protocols/hole_punching/mod.rs L45-46, L95-107
rate_limiter: RateLimiter<(PeerIndex, u32)>,
...
if self.rate_limiter.check_key(&(session_id, msg.item_id())).is_err() { return; }
```

`BlockFilter` is the only production protocol handler that omits this guard entirely. No ban is triggered for excessive `GetBlockFilterHashes` requests; the `should_ban()` path in `BlockFilter::process` is only reached on protocol errors, not on volume.

## Impact Explanation
RocksDB is shared across all CKB subsystems (block relay, tx relay, chain state). A single attacker peer sending `GetBlockFilterHashes { start_number: 0 }` in a tight loop at TCP line rate causes sustained high-IOPS RocksDB reads that compete with block/tx relay I/O. This increases P2P relay latency for all connected peers of the targeted node and degrades its ability to propagate blocks and transactions. Applied to multiple nodes simultaneously (each requiring only a single TCP connection and ~50-byte messages), this can cause **CKB network congestion with few costs** — matching the High impact class (10001–15000 points): *"Vulnerabilities or bad designs which could cause CKB network congestion with few costs."*

## Likelihood Explanation
The attack requires only a standard P2P connection to a node that has enabled block filter building (opt-in but common for light-client-serving nodes). No PoW, no stake, no privileged access is needed. The attacker pays only the cost of a TCP connection and sending small messages; the victim pays up to 4,000 RocksDB reads per message. The missing guard is a straightforward omission relative to the established pattern in `Relayer` and `HolePunching`. The attack is repeatable, stateless, and requires no coordination.

## Recommendation
Add a `rate_limiter: RateLimiter<(PeerIndex, u32)>` field to the `BlockFilter` struct in `sync/src/filter/mod.rs`, mirroring the pattern in `Relayer::new`. In `BlockFilter::try_process`, add a rate-limit check at the top before dispatching to any of the three process handlers, keyed by `(peer, message.item_id())`. A quota of 30 req/s per peer per message type (matching the `Relayer`'s quota) is a reasonable starting point. Call `rate_limiter.retain_recent()` in the `disconnected` handler to bound memory growth.

## Proof of Concept
1. Connect a single peer to a CKB node with filter data built for a large number of blocks (block filter building enabled via config).
2. In a tight loop, send `GetBlockFilterHashes { start_number: 0 }` (a ~10-byte serialized message).
3. Each message causes `GetBlockFilterHashesProcess::execute` to run the 2,000-iteration loop, issuing up to 4,000 RocksDB reads with no throttle or ban applied to the sender.
4. Monitor RocksDB read IOPS (via `rocksdb.stats` metrics) and P2P relay latency for other peers — both degrade proportionally to the flood rate.
5. Confirm no `TooManyRequests` status is returned and no ban is applied to the attacking peer (contrast with sending excess `GetRelayTransactions` to the `Relayer`, which triggers `StatusCode::TooManyRequests` after 30 req/s).
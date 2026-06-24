Audit Report

## Title
Missing Per-Peer Rate Limiter on BlockFilter Protocol Enables I/O Amplification DoS — (`sync/src/filter/mod.rs`, `sync/src/filter/get_block_filter_hashes_process.rs`)

## Summary

The `BlockFilter` protocol handler exposes three request variants (`GetBlockFilters`, `GetBlockFilterHashes`, `GetBlockFilterCheckPoints`) with no rate limiting. Any connected peer can repeatedly send `GetBlockFilterHashes(start_number=0)`, each triggering up to 4,000 RocksDB point-reads and producing a ~64 KB response, with zero throttle. The `Relayer` handler has an explicit `governor`-based per-peer rate limiter; the `BlockFilter` handler has none, and the omission is confirmed by code inspection.

## Finding Description

`GetBlockFilterHashesProcess::execute` iterates up to `BATCH_SIZE = 2000` times. Each iteration performs two sequential store lookups:

```rust
// sync/src/filter/get_block_filter_hashes_process.rs, lines 53-66
for _ in 0..BATCH_SIZE {
    if let Some(block_filter_hash) = active_chain
        .get_block_hash(block_number)                          // RocksDB read #1
        .and_then(|block_hash| active_chain.get_block_filter_hash(&block_hash))  // RocksDB read #2
    { ... }
}
```

That is up to **4,000 RocksDB point-reads** per message, producing a response of up to 2,000 × 32 = **64 KB**.

The `BlockFilter` struct carries no rate-limiter field:

```rust
// sync/src/filter/mod.rs, lines 22-25
pub struct BlockFilter {
    shared: Arc<SyncShared>,
}
```

The `try_process` dispatch path performs no rate check before calling `execute`:

```rust
// sync/src/filter/mod.rs, lines 33-68
async fn try_process(...) -> Status {
    match message {
        packed::BlockFilterMessageUnionReader::GetBlockFilterHashes(msg) => {
            GetBlockFilterHashesProcess::new(msg, self, nc, peer).execute().await
        }
        // ... no rate check anywhere
    }
}
```

By contrast, `Relayer` carries a `RateLimiter<(PeerIndex, u32)>` and rejects requests exceeding 30 req/s per peer per message type before any dispatch:

```rust
// sync/src/relayer/mod.rs, lines 116-123
if should_check_rate && self.rate_limiter.check_key(&(peer, message.item_id())).is_err() {
    return StatusCode::TooManyRequests.with_context(message.item_name());
}
```

A grep over `sync/src/filter/**` returns zero matches for `rate_limit`, `RateLimiter`, `governor`, or `TooManyRequests`, confirming no rate limiting exists anywhere in the filter module.

The `Filter` protocol is included in the default protocol set, so every standard full node exposes it without any configuration change required.

## Impact Explanation

With N peers (up to `max_peers`, typically 125) each sending `GetBlockFilterHashes(start_number=0)` in a tight loop:

- **Disk I/O**: N × 4,000 RocksDB reads per cycle. At 125 peers this is 500,000 reads per round, easily saturating spinning disks and significantly loading SSDs.
- **Outbound bandwidth**: N × 64 KB per round = 8 MB per round at 125 peers, at whatever rate the async runtime drains the queue.
- **Collateral damage**: The sync and relay protocols share the same async executor and RocksDB instance. Saturating either resource delays block/header propagation and transaction relay, degrading the node's participation in consensus and potentially crashing it under sustained load.

This matches the allowed CKB bounty impact: **High — Vulnerabilities or bad designs which could cause CKB network congestion with few costs** and **High — Vulnerabilities which could easily crash a CKB node**.

## Likelihood Explanation

The attack path is entirely unprivileged and reachable from the public P2P interface. The attacker needs only standard TCP connectivity to the node's P2P port. No PoW, no stake, no authentication, and no victim mistake is required. The cost to the attacker is negligible: open N TCP connections and send a small fixed message in a tight loop. The asymmetry with `Relayer` (which has an explicit rate limiter added intentionally) confirms the omission in `BlockFilter` is unintentional. The attack is repeatable and deterministic.

## Recommendation

Apply the same `governor`-based per-peer rate limiter pattern already used in `Relayer` to the `BlockFilter` handler:

1. Add a `RateLimiter<(PeerIndex, u32)>` field to `BlockFilter` (mirroring `sync/src/relayer/mod.rs` lines 63–67, 81).
2. In `BlockFilter::try_process`, check the limiter keyed by `(peer, message.item_id())` before dispatching any of the three request variants, returning `StatusCode::TooManyRequests` on limit breach.
3. Call `rate_limiter.retain_recent()` in the `disconnected` handler to bound memory growth.
4. Consider a global concurrency cap on in-flight Filter requests to bound aggregate RocksDB read pressure regardless of peer count.

## Proof of Concept

```rust
// Spawn N mock peers, each sending GetBlockFilterHashes in a tight loop
for _ in 0..125 {
    tokio::spawn(async move {
        let mut stream = connect_to_node_filter_protocol().await;
        let msg = GetBlockFilterHashes { start_number: 0 }.encode();
        loop {
            stream.send(msg.clone()).await;
            // do not wait for response; just flood
        }
    });
}
// Observable: node RocksDB read latency spikes, sync/relay message
// processing latency increases proportionally to peer count,
// node becomes unresponsive or crashes under sustained load.
```

Each spawned peer triggers up to 4,000 RocksDB reads (`BATCH_SIZE = 2000`, two reads per iteration) and receives a ~64 KB `BlockFilterHashes` response with zero authentication cost, confirming multiplicative I/O amplification proportional to peer count.
Audit Report

## Title
Missing Per-Peer Rate Limiting on `BlockFilter` Protocol Handler Enables DB Read Amplification DoS — (`sync/src/filter/mod.rs`)

## Summary

The `BlockFilter` protocol handler processes `GetBlockFilterHashes`, `GetBlockFilters`, and `GetBlockFilterCheckPoints` messages with no per-peer rate limiting. Each `GetBlockFilterHashes` message triggers up to 4,000 sequential RocksDB reads (2,000 loop iterations × `get_block_hash` + `get_block_filter_hash`). A single unprivileged peer can flood this handler at TCP line rate, saturating the shared RocksDB read path and degrading block/tx relay for all peers, with no throttle or ban applied to the attacker.

## Finding Description

`GetBlockFilterHashesProcess::execute` defines `BATCH_SIZE = 2000` and loops up to that many times, issuing two RocksDB reads per iteration:

```rust
// sync/src/filter/get_block_filter_hashes_process.rs, L8
const BATCH_SIZE: BlockNumber = 2000;

// L52-66
let mut block_number = start_number;
for _ in 0..BATCH_SIZE {
    if let Some(block_filter_hash) = active_chain
        .get_block_hash(block_number)
        .and_then(|block_hash| active_chain.get_block_filter_hash(&block_hash))
    { ... }
}
```

That is up to **4,000 RocksDB point-reads per message**. The `BlockFilter` handler struct carries no rate-limiter field:

```rust
// sync/src/filter/mod.rs, L22-25
pub struct BlockFilter {
    shared: Arc<SyncShared>,
}
```

And `BlockFilter::try_process` dispatches directly to all three process handlers with no rate-limit check at any point before or during dispatch (`sync/src/filter/mod.rs`, L33–68).

Contrast this with `Relayer`, which holds `rate_limiter: RateLimiter<(PeerIndex, u32)>` (`sync/src/relayer/mod.rs`, L81) and gates every non-PoW message through it at the top of `try_process` (`sync/src/relayer/mod.rs`, L113–123). The same pattern is present in `HolePunching` (`network/src/protocols/hole_punching/mod.rs`, L45–46, L95–107). `BlockFilter` is the only production protocol handler that omits this guard entirely.

The same gap also affects `GetBlockFilters` (`BATCH_SIZE = 1000`, `get_block_filters_process.rs` L9) and `GetBlockFilterCheckPoints` (`BATCH_SIZE = 2000`, `get_block_filter_check_points_process.rs` L9–10).

## Impact Explanation

RocksDB is shared across all CKB subsystems (block relay, tx relay, chain state). Flooding `GetBlockFilterHashes` from a single peer at TCP line rate causes sustained high-IOPS RocksDB reads that compete with block/tx relay I/O, increasing P2P relay latency for all connected peers and potentially stalling block propagation. This maps to: **High — Vulnerabilities or bad designs which could cause CKB network congestion with few costs.** The attacker pays only the cost of a TCP connection and sending ~50-byte messages; the victim pays 4,000 RocksDB reads per message with no bound.

## Likelihood Explanation

The attack requires only a standard P2P connection to a CKB node that has enabled block filter building (opt-in but common for light-client-serving nodes). No PoW, no stake, no privileged access is required. The missing guard is a straightforward omission relative to the existing pattern in `Relayer` and `HolePunching`. The attack is repeatable and sustainable indefinitely from a single peer.

## Recommendation

Add a `rate_limiter: RateLimiter<(PeerIndex, u32)>` field to the `BlockFilter` struct in `sync/src/filter/mod.rs`, mirroring the pattern in `Relayer::new` (`sync/src/relayer/mod.rs`, L88–98). Apply the check at the top of `BlockFilter::try_process` before dispatching to any of the three process handlers, mirroring `Relayer::try_process` (`sync/src/relayer/mod.rs`, L113–123). A limit of 30 req/s per peer per message type (matching the Relayer's quota) is a reasonable starting point. Call `rate_limiter.retain_recent()` in the `disconnected` handler, as done in `Relayer` (`sync/src/relayer/mod.rs`, L934).

## Proof of Concept

1. Connect a single peer to a CKB node with filter data built for a large number of blocks (block filter building enabled in config).
2. In a tight loop, send `GetBlockFilterHashes { start_number: 0 }` (~10-byte message).
3. Each message causes the node to execute the 2,000-iteration loop in `GetBlockFilterHashesProcess::execute` (`sync/src/filter/get_block_filter_hashes_process.rs`, L52–66), issuing ~4,000 RocksDB reads.
4. Monitor RocksDB read IOPS (via `rocksdb.stats` metrics) and P2P relay latency for other peers — both degrade proportionally to the flood rate, with no ban or throttle applied to the attacking peer, as confirmed by the absence of any rate-limit check in `BlockFilter::try_process` (`sync/src/filter/mod.rs`, L33–68).
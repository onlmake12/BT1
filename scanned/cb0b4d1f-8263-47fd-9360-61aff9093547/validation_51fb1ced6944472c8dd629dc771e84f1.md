Audit Report

## Title
Unbounded DB Read Amplification via Unauthenticated `GetBlockFilterHashes` with No Per-Peer Rate Limiting — (`sync/src/filter/get_block_filter_hashes_process.rs`)

## Summary

`GetBlockFilterHashesProcess::execute` performs up to 4002 RocksDB reads per well-formed P2P message (2 parent lookups + up to 2000×2 batch reads). Unlike the `Relayer` and `HolePunching` protocol handlers, `BlockFilter` has no per-peer rate limiter. Any connected peer can flood the victim with `GetBlockFilterHashes` messages in a tight loop, causing unbounded DB read amplification with no ban or backpressure applied.

## Finding Description

**Root cause**: `BlockFilter` lacks the `rate_limiter` field and rate-check gate present in both `Relayer` and `HolePunching`.

**Code path**:

1. `BlockFilter::received` (`sync/src/filter/mod.rs:122-152`) parses the message and calls `self.process(nc, peer_index, msg)` with no rate check.
2. `BlockFilter::process` (`mod.rs:70-115`) calls `try_process`, which dispatches to `GetBlockFilterHashesProcess::execute`.
3. `execute` (`get_block_filter_hashes_process.rs:32-80`) performs:
   - `get_block_hash(start_number - 1)` + `get_block_filter_hash(...)` → 2 DB reads (parent lookup)
   - Loop up to `BATCH_SIZE = 2000` iterations, each doing `get_block_hash` + `get_block_filter_hash` → up to 4000 DB reads
   - **Total: up to 4002 DB reads per message**

**Why existing checks fail**:

- The only ban path in `received` is for unparseable bytes (`BAD_MESSAGE_BAN_TIME`). A well-formed `GetBlockFilterHashes{start_number: 1}` never triggers `status.should_ban()`.
- `execute` returns `Status::ignored()` only when `latest < start_number` or a parent hash is missing — both conditions are trivially avoided on a synced mainnet node.
- Contrast with `Relayer` (`sync/src/relayer/mod.rs:81,116-123`), which has `rate_limiter: RateLimiter<(PeerIndex, u32)>` and checks it in `try_process` before any processing, and `HolePunching` (`network/src/protocols/hole_punching/mod.rs:45,95-107`), which does the same. `BlockFilter` has neither.

## Impact Explanation

**High — Vulnerabilities or bad designs which could cause CKB network congestion with few costs.**

A single attacker peer can saturate the victim node's RocksDB read I/O by sending `GetBlockFilterHashes` in a tight loop. Each small P2P message (a few bytes) triggers up to 4002 DB reads. Saturated I/O starves block processing, header sync, and transaction relay, causing the node to fall behind chain tip and degrade its participation in block propagation. The amplification ratio (~4002 DB reads per message) and zero attacker cost (no ban, no backpressure, no sleep required) make this a practical high-severity DoS.

## Likelihood Explanation

- **Precondition**: The node has built ≥1 filter block — trivially true on mainnet.
- **Attacker capability**: Any peer that completes the standard P2P handshake; no privilege required.
- **Repeatability**: Indefinite — no ban is ever triggered for well-formed messages, no cooldown or quota exists.
- **Cost**: Negligible — a single connection sending a tight loop of identical small messages suffices.

## Recommendation

1. Add a `rate_limiter: RateLimiter<(PeerIndex, u32)>` field to `BlockFilter` (mirroring `Relayer` and `HolePunching`).
2. In `BlockFilter::received` or `try_process` (`sync/src/filter/mod.rs`), check the rate limiter before dispatching to any `execute` handler; return `StatusCode::TooManyRequests` (or `Status::ignored()` with a cooldown) on excess.
3. Apply the same rate-limiting gate to `GetBlockFilters` and `GetBlockFilterCheckPoints`, which have the same structural issue.

## Proof of Concept

```
# Minimal PoC (pseudocode)
peer.connect(victim_node)  # complete standard P2P handshake
loop:
    peer.send(GetBlockFilterHashes { start_number: 1 })
    # No sleep — no ban, no backpressure
    # Victim performs up to 4002 RocksDB reads per iteration
    # Victim sends back BlockFilterHashes(2000 hashes) — attacker discards it

# Observable: victim node's RocksDB read IOPS scales linearly with send rate;
# no ban event logged; no rate-limit response received.
```

**Verification steps**:
1. Run a CKB node with block filter enabled and ≥1 filter block built.
2. Connect a test peer and send `GetBlockFilterHashes{start_number: 1}` in a tight loop.
3. Monitor RocksDB read metrics (e.g., via Prometheus or `rocksdb.stats`): IOPS will scale linearly with message rate.
4. Confirm no ban is issued to the test peer and no rate-limit response is returned.
5. Observe block sync and relay latency degradation under load.
Audit Report

## Title
`check_purge` Second-Pass Eviction Blind Spot Allows Peer Store Lockout via Crafted Address Distribution — (File: `network/src/peer_store/peer_store_impl.rs`)

## Summary
The `check_purge` function's second-pass eviction uses a strict `> 4` guard at line 378. An attacker who fills the peer store with exactly 4 connectable addresses per /16 network group across 4096 groups (totalling `ADDR_COUNT_LIMIT = 16384`) causes every subsequent `add_addr` call to return `Err(EvictionFailed)`, permanently blocking new peer discovery until the node exhausts connection attempts against the fake addresses.

## Finding Description
`check_purge` is invoked by `add_addr` before inserting any new address. It has two eviction passes.

**Pass 1** collects addresses where `is_connectable` returns `false`. A freshly-advertised `AddrInfo` with `last_connected_at_ms=0` and `attempts_count=0` passes all three guards in `is_connectable` and returns `true` (the condition `last_connected_at_ms == 0 && attempts_count >= ADDR_MAX_RETRIES` requires `attempts_count >= 3`, which is not satisfied). Pass 1 finds zero candidates.

**Pass 2** groups addresses by `Group`, sorts groups by descending size, takes the top `len/2` groups, and for each group applies:

```rust
if addrs.len() > 4 {   // line 378
    Some(...)
} else {
    None
}
```

With 4096 groups of exactly 4 peers each: `len=4096`, `take(2048)` iterates 2048 groups, but `4 > 4` is `false` for every group. Every group returns `None`, `candidate_peers` is empty, and line 399–400 returns `Err(PeerStoreError::EvictionFailed)`.

The grouping key is confirmed as `Group::IP4([bits[0], bits[1]])` — the first two octets of the IPv4 address (a /16-equivalent, not /24).

All three ingestion paths silently swallow this error: Discovery logs at `debug` level and continues; Identify logs at `error` level and continues; DNS seeding uses `let _ = ...` discarding the result entirely.

## Impact Explanation
Once the store is locked, no new peer addresses can be added via Discovery, Identify, or DNS seeding. `fetch_addrs_to_feeler` and `fetch_addrs_to_attempt` continue to operate only on existing attacker-controlled entries. The node cannot expand its peer set or recover from peer churn without operator intervention. This constitutes a targeted node isolation / peer-discovery denial-of-service, matching **High: Vulnerabilities or bad designs which could cause CKB network congestion with few costs** — a single attacker connection can lock out a node's peer discovery indefinitely.

## Likelihood Explanation
The attacker requires only a single established connection to the target node. The Discovery protocol accepts addresses from connected peers via `Nodes` messages with no per-session cap on total addresses contributed. `is_valid_addr` only checks global routability, not actual reachability, so the attacker does not need to control the 16384 advertised IPs — any valid public addresses across 4096 distinct /16 networks suffice. The addresses can be sent across multiple `Nodes` messages over time. The state persists until the node accumulates ≥3 failed connection attempts per address (slow, and the attacker can re-fill before recovery completes).

## Recommendation
Change the strict inequality on line 378 from `> 4` to `>= 4`:

```rust
// Before
if addrs.len() > 4 {

// After
if addrs.len() >= 4 {
```

This ensures groups of exactly 4 are eligible for eviction in the second pass. Additionally, consider adding a per-session rate limit on the total number of addresses a single peer may contribute to the store, and consider adding a minimum eviction guarantee (e.g., always evict at least one address when the store is full).

## Proof of Concept
In `network/src/tests/peer_store.rs`, add a test that:
1. Constructs a `PeerStore` and inserts 16384 `AddrInfo` entries: 4 addresses per /16 group across 4096 groups (e.g., `1.0.0.1` through `16.255.x.y`), all with `last_connected_at_ms=0` and `attempts_count=0`.
2. Calls `peer_store.add_addr(fresh_addr, Flags::COMPATIBILITY)`.
3. Asserts the result is `Err(PeerStoreError::EvictionFailed)`.

The existing `test_eviction` test confirms the second-pass path works when groups exceed 4, but no test covers the exactly-4 boundary case, leaving this blind spot undetected.
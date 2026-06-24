Audit Report

## Title
`check_purge` Second-Pass Eviction Blind Spot Allows Peer Store Lockout via Crafted Address Distribution — (`network/src/peer_store/peer_store_impl.rs`)

## Summary
The `check_purge` function's second eviction pass uses a strict `> 4` guard at line 378. An attacker who fills the peer store with exactly 4 connectable addresses per /16 network group across 4096 groups (totalling `ADDR_COUNT_LIMIT = 16384`) causes every subsequent `add_addr` call to return `Err(EvictionFailed)`, permanently blocking new peer discovery for the lifetime of the node until the attacker-controlled entries age out or accumulate enough failed connection attempts.

## Finding Description

`add_addr` calls `check_purge()` before inserting any address (`peer_store_impl.rs` line 75). `check_purge` has two eviction passes:

**Pass 1** (lines 341–355): Collects addresses where `!is_connectable(now_ms)`. A freshly-advertised `AddrInfo` has `last_connected_at_ms=0`, `attempts_count=0`, and `last_tried_at_ms=0`. In `is_connectable` (`types.rs` lines 89–105):
- `tried_in_last_minute` is false (0 is not ≥ now_ms − 60000 for any real timestamp)
- `last_connected_at_ms == 0 && attempts_count >= ADDR_MAX_RETRIES(3)` → `0 >= 3` is false
- Returns `true`

So Pass 1 finds zero candidates when all 16384 entries are freshly advertised.

**Pass 2** (lines 357–401): Groups addresses by `Group::IP4([bits[0], bits[1]])` (first two octets, confirmed in `network_group.rs` lines 26–28). With 4096 groups of exactly 4 peers each:
- `len = 4096`, `take(len / 2)` = `take(2048)` — iterates 2048 groups
- Guard at line 378: `if addrs.len() > 4` → `4 > 4` is **false** for every group
- Every group returns `None`; `candidate_peers` is empty
- Line 399–400: returns `Err(PeerStoreError::EvictionFailed)`

All three ingestion paths silently swallow this error:
- Discovery (`discovery/mod.rs` lines 354–361): logs at `debug!` level and continues
- Identify (`identify/mod.rs` lines 488–494): logs at `error!` level and continues
- DNS seeding (`dns_seeding/mod.rs` lines 110–114): `let _ = peer_store.add_addr(...)` discards the error entirely

## Impact Explanation

Once the store is locked, no new peer addresses can be added via Discovery, Identify, or DNS seeding. `fetch_addrs_to_feeler` and `fetch_addrs_to_attempt` continue to operate only on the attacker-controlled entries, directing the node's outbound connection attempts exclusively toward addresses the attacker chose. The node cannot expand its peer set or recover from peer churn without a restart. At scale, this constitutes a low-cost mechanism to fragment the CKB peer network, fitting the **High** impact class: "Vulnerabilities or bad designs which could cause CKB network congestion with few costs."

## Likelihood Explanation

The attacker requires only a single connected peer session. The Discovery protocol's `add_new_addrs` path accepts addresses from `Nodes` messages without verifying reachability at ingestion time — `is_valid_addr` only checks global routability, which any valid public IP satisfies. There is no per-session cap on total addresses contributed. A single malicious peer can send multiple `Nodes` messages over time to fill all 16384 slots across 4096 /16 groups (e.g., `1.0.x.y` through `16.0.x.y`, 4 addresses per /16). The attacker does not need to control those IPs. The condition is stable: the attacker can re-fill the store if entries are evicted by connection failures, since the failure accumulation rate (≥3 failed attempts per address) is slow and controllable.

## Recommendation

Change the strict inequality at line 378 from `> 4` to `>= 4` (equivalently `> 3`):

```rust
// Before
if addrs.len() > 4 {

// After
if addrs.len() >= 4 {
```

This ensures groups of exactly 4 are eligible for eviction in the second pass, closing the boundary blind spot. Additionally, consider adding a per-session rate limit on the total number of addresses a single peer may contribute to the store to raise the cost of this attack.

## Proof of Concept

Using the existing `#[cfg(feature = "fuzz")] add_addr_fuzz` path or direct `addr_manager` manipulation, pre-fill the store with 16384 `AddrInfo` entries: 4 per /16 group across 4096 groups (e.g., addresses `A.B.x.y` for A in 1..=16, B in 0..=255, with 4 distinct `x.y` per group), all with `last_connected_at_ms=0` and `attempts_count=0`. Then call `add_addr` with a fresh address and assert the result is `Err(EvictionFailed)`. The existing `test_eviction` test in `network/src/tests/peer_store.rs` (lines 496–590) confirms the second-pass path works when groups exceed 4, but no test covers the exactly-4 boundary case, leaving this blind spot untested.
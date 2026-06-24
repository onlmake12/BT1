Audit Report

## Title
Peer Store Eviction Permanently Blocked via Crafted Address Distribution — (`network/src/peer_store/peer_store_impl.rs`)

## Summary
`check_purge` uses a strict `> 4` threshold when selecting eviction candidates from network groups. An attacker who fills the peer store with exactly 4 connectable addresses per `/16` group causes both eviction stages to produce empty candidate sets, returning `EvictionFailed` on every subsequent `add_addr` call. Once triggered, the node cannot record any new peer address discovered via the P2P discovery protocol.

## Finding Description
`check_purge` at `network/src/peer_store/peer_store_impl.rs:327` has two eviction stages:

**Stage 1 (lines 341–355):** Collects addresses where `is_connectable(now_ms)` returns `false`. `AddrInfo::new` initializes `last_connected_at_ms = 0` and `attempts_count = 0` (`types.rs:65–76`). With `attempts_count = 0`, neither non-connectable condition in `is_connectable` (`types.rs:95`, `types.rs:99–101`) is satisfied, so freshly-added addresses are always connectable and stage 1 removes nothing.

**Stage 2 (lines 358–401):** Groups addresses by `Group::IP4([bits[0], bits[1]])` (`network_group.rs:26–28`), i.e., by `/16` prefix. It sorts groups by descending size, takes the top `len/2`, and for each group evicts 2 peers **only if `addrs.len() > 4`** (line 378). If every group contains exactly 4 entries, this condition is `false` for every group, `candidate_peers` remains empty, and line 399–401 returns `Err(PeerStoreError::EvictionFailed)`.

This error propagates through `add_addr` (line 75, `?` operator), causing every subsequent `add_addr` call to fail. The discovery handler at `protocols/discovery/mod.rs:354–361` silently logs the error and continues, so the node never panics but permanently stops recording new peer addresses.

The `is_valid_addr` filter in `DiscoveryAddressManager` (`mod.rs:332–341`) only rejects non-globally-reachable IPs; an attacker can advertise arbitrary public IPs they do not own. With `16384 / 4 = 4096` distinct `/16` groups needed, and IPv4 having 65536 possible `/16` blocks, the attacker has ample address space to construct the adversarial distribution.

## Impact Explanation
Once the store is locked, the node cannot learn about new peers via discovery. Its peer set becomes permanently stale, degrading block and transaction propagation and enabling eclipse-like isolation from the honest network. This constitutes a bad design that impairs CKB network health with minimal attacker cost, matching the **High** impact class: *Vulnerabilities or bad designs which could cause CKB network congestion with few costs*.

## Likelihood Explanation
The attack requires only a single connected peer sending crafted `Nodes` discovery messages advertising public IP addresses distributed as exactly 4 per `/16` group. No PoW, key material, or operator access is needed. The `verify_nodes_message` check limits items per message to `MAX_ADDR_TO_SEND = 1000`, but the attacker can send multiple messages across multiple sessions over time to fill the 16384-entry store. The attack is repeatable and persistent across node restarts if the peer store is persisted to disk.

## Recommendation
Change the eviction threshold at `peer_store_impl.rs:378` from `addrs.len() > 4` to `addrs.len() >= 4` (or `> 1`) so that any group with more than one entry is eligible for eviction when the store is full. Additionally, add an unconditional fallback that evicts the oldest or lowest-scored address when both stages produce empty candidate sets, guaranteeing `check_purge` always succeeds when the store is at capacity.

## Proof of Concept
```rust
// Fill store: 4096 groups × 4 addresses = 16384 = ADDR_COUNT_LIMIT
for group_id in 0u16..4096 {
    let [hi, lo] = group_id.to_be_bytes();
    for host in 1u8..=4 {
        let addr = format!("/ip4/{}.{}.0.{}/tcp/8115/p2p/Qm...", hi, lo, host)
            .parse::<Multiaddr>().unwrap();
        peer_store.add_addr(addr, Flags::COMPATIBILITY).unwrap();
    }
}
// Store is now at ADDR_COUNT_LIMIT; every /16 group has exactly 4 connectable entries.
// Stage 1: all entries have attempts_count=0, so none are non-connectable → removes nothing.
// Stage 2: all groups have len==4, so addrs.len() > 4 is false for all → removes nothing.
let new_addr = "/ip4/200.1.0.1/tcp/8115/p2p/Qm...".parse().unwrap();
let result = peer_store.add_addr(new_addr, Flags::COMPATIBILITY);
assert!(matches!(result, Err(e) if e.to_string().contains("EvictionFailed")));
```
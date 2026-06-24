Audit Report

## Title
Peer Store Phase-2 Eviction Always Fails When All Addresses Share One /16 Network Group Due to `take(len / 2)` Integer Division — (`network/src/peer_store/peer_store_impl.rs`)

## Summary
In `check_purge()`, phase-2 eviction groups addresses by `/16` network segment and calls `.take(len / 2)` on the sorted groups. When all stored addresses belong to exactly one `/16` group, `len == 1` and integer division yields `take(0)`, producing an empty iterator. `candidate_peers` is empty, and the function returns `Err(PeerStoreError::EvictionFailed)` on every subsequent `add_addr()` call. An attacker who fills the 16384-slot store with addresses all in one `/16` block triggers this path continuously, blocking the victim node from adding any newly discovered peer addresses.

## Finding Description
`check_purge()` runs in two phases when `addr_manager.count() >= ADDR_COUNT_LIMIT` (16384):

**Phase 1** (lines 341–355): Evict addresses where `is_connectable()` returns `false`. Freshly injected addresses have `last_connected_at_ms = 0` and `attempts_count = 0`, so `is_connectable()` returns `true` for all of them — phase 1 finds nothing to evict.

**Phase 2** (lines 357–401): Group all addresses by `/16` network segment (`Group::IP4([bits[0], bits[1]])`), sort groups by size descending, then call `.take(len / 2)` where `len` is the number of distinct groups. When all 16384 addresses are in one `/16` block (e.g., `1.2.0.0/16`), `len == 1` and `1 / 2 == 0` (Rust integer division), so `take(0)` produces an empty iterator. The `flat_map` never executes, `candidate_peers` is empty, and the function returns `Err(PeerStoreError::EvictionFailed)`.

The error is silently swallowed at `debug` level in the discovery handler (lines 355–360 of `discovery/mod.rs`), so the victim node receives no warning.

**Root cause**: `take(len / 2)` at `peer_store_impl.rs:376` truncates to zero when `len == 1`.

**Existing guards that fail**:
- `is_valid_addr` requires globally reachable IPs, but a single `/16` block provides 65,536 IPs — far more than 16,384 needed.
- `verify_nodes_message` caps non-announce Nodes messages at `MAX_ADDR_TO_SEND = 1000` items, but 17 sessions × 1000 addresses = 17,000 > 16,384, so the store fills completely.
- The `DuplicateFirstNodes` misbehavior check only prevents a second non-announce message per session; announce messages (up to `ANNOUNCE_THRESHOLD = 10` per 60s interval) can supplement.

## Impact Explanation
While the store is locked, the victim node cannot add any newly discovered peer addresses from the discovery protocol or the identify protocol. The victim's peer discovery is severely degraded: it cannot learn about new peers and is limited to its existing (attacker-controlled) address set. If applied to multiple nodes simultaneously, this constitutes **CKB network congestion with few costs** (High, 10001–15000 points), as peer routing and propagation degrade across the network. At minimum it is a sustained DoS on peer discovery for individual nodes.

## Likelihood Explanation
Requires approximately 17 inbound sessions (within default inbound peer limits), each sending 1000 addresses via a single non-announce `Nodes` message, all advertising addresses in the same `/16` block. No special privileges are needed — any peer that can establish a P2P connection can execute this. The attack must be sustained: as the victim marks fake addresses non-connectable after 3 failed attempts (`ADDR_MAX_RETRIES = 3`), phase-1 eviction eventually frees slots, so the attacker must continuously re-inject addresses to maintain the block.

## Recommendation
Replace `take(len / 2)` with `take((len + 1) / 2)` or `take(len.max(1))` to ensure at least one group is processed when `len >= 1`. Additionally, consider rate-limiting the number of addresses accepted per session and per source `/16` group before they reach the peer store, to reduce the feasibility of filling the store from a small number of sessions.

## Proof of Concept
```rust
#[test]
fn test_eviction_fails_single_network_group() {
    let mut peer_store = PeerStore::default();
    // Fill store with 16384 addresses all in 1.2.0.0/16
    for i in 0u32..16384 {
        let ip = std::net::Ipv4Addr::new(1, 2, (i / 256) as u8, (i % 256) as u8);
        let peer_id = PeerId::random();
        let addr: Multiaddr = format!("/ip4/{}/tcp/8114/p2p/{}", ip, peer_id.to_base58())
            .parse().unwrap();
        peer_store.add_addr(addr, Flags::COMPATIBILITY).unwrap();
    }
    // Now every add_addr returns EvictionFailed
    let peer_id = PeerId::random();
    let new_addr: Multiaddr = format!("/ip4/3.4.5.6/tcp/8114/p2p/{}", peer_id.to_base58())
        .parse().unwrap();
    assert!(matches!(
        peer_store.add_addr(new_addr, Flags::COMPATIBILITY),
        Err(e) if e.to_string().contains("EvictionFailed")
    ));
}
```
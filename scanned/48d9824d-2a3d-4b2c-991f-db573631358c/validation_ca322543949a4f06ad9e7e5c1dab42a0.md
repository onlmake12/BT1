All four code citations in the submission have been verified against the actual source. The behavior is exactly as described:

All code citations verified. Additionally, `accept_peer` in `peer_registry.rs` at line 109 also calls `peer_store.is_addr_banned(&remote_addr)` to gate inbound connections — meaning the bypass extends to inbound reconnection as well, not just `add_addr` re-insertion.

---

Audit Report

## Title
Onion3 (Tor) Peers Silently Bypass Score-Based Banning, Enabling Indefinite Reconnection — (`network/src/peer_store/peer_store_impl.rs`)

## Summary
`ban_addr` fails to insert Onion3 multiaddrs into the ban list because `multiaddr_to_socketaddr` returns `None` for them, causing the `if let Some(...)` guard to skip `ban_network`. Since `is_addr_banned` uses the same conversion and returns `false` for all Onion3 addresses, a "banned" Tor peer is never actually blocked: it can reconnect inbound (checked in `accept_peer`) and be re-added via discovery (checked in `add_addr`) with no restriction.

## Finding Description
In `peer_store_impl.rs`, `ban_addr` is:

```rust
pub(crate) fn ban_addr(&mut self, addr: &Multiaddr, timeout_ms: u64, ban_reason: String) {
    if let Some(addr) = multiaddr_to_socketaddr(addr) {   // None for Onion3 → skipped
        let network = ip_to_network(addr.ip());
        self.ban_network(network, timeout_ms, ban_reason)  // never called
    }
    self.addr_manager.remove(addr);                        // always called
}
```

For an Onion3 multiaddr, `multiaddr_to_socketaddr` returns `None`, so `ban_network` is never called and no entry is written to `ban_list`. The peer is evicted from `addr_manager` but the ban list remains empty for that peer.

`is_addr_banned` in `ban_list.rs` has the same blind spot:

```rust
pub fn is_addr_banned(&self, addr: &Multiaddr) -> bool {
    multiaddr_to_socketaddr(addr)
        .map(|socket_addr| self.is_ip_banned(&socket_addr.ip()))
        .unwrap_or_default()   // always false for Onion3
}
```

This function is called in two critical enforcement points:

1. **`add_addr`** (`peer_store_impl.rs` L72): gates re-insertion of discovered addresses — always passes for Onion3.
2. **`accept_peer`** (`peer_registry.rs` L109): gates inbound connection acceptance — always passes for Onion3.

Both checks are bypassed, meaning a "banned" Tor peer can reconnect inbound immediately and be re-added to `addr_manager` via discovery without restriction.

`fetch_random` in `addr_manager.rs` (L74–90) explicitly special-cases Onion3 in the `None` branch of `multiaddr_to_socketaddr`, confirming Tor is an intentionally supported production transport — not an edge case.

The full exploit path:
1. Tor peer connects (inbound or outbound).
2. Peer sends misbehaving data (e.g., invalid compact block, malformed header) triggering `report()` → score drops below `ban_score`.
3. `ban_addr` is called: peer is removed from `addr_manager`, but ban list is never updated.
4. Peer immediately reconnects inbound — `accept_peer` calls `is_addr_banned` which returns `false`.
5. Peer is re-added to `addr_manager` via discovery — `add_addr` calls `is_addr_banned` which returns `false`.
6. Steps 2–5 repeat indefinitely with zero cumulative penalty.

## Impact Explanation
The score-based misbehavior enforcement system is completely ineffective for the entire class of Onion3 peers. A misbehaving Tor peer can send invalid blocks, headers, or transactions, trigger the ban path, reconnect immediately, and repeat indefinitely. With multiple Tor circuits (which are free), an attacker can maintain a sustained stream of invalid data against a node, consuming connection slots, CPU (for validation), and bandwidth with no cost-based deterrent. This matches **High: Vulnerabilities or bad designs which could cause CKB network congestion with few costs** (10001–15000 points), since Tor is free, circuits are trivially rotated, and the protection mechanism provides zero resistance.

## Likelihood Explanation
Onion3 is explicitly supported as a production transport — `fetch_random` contains a dedicated code path that includes Onion3 addresses in connection candidates even when `multiaddr_to_socketaddr` returns `None`. Any unprivileged peer reachable over Tor that can trigger a negative score report (e.g., by sending invalid compact block data or a malformed header) can exploit this. No special privileges, victim mistakes, or external dependencies are required. The attack is repeatable indefinitely.

## Recommendation
`ban_addr` must handle non-IP transports explicitly. For Onion3 addresses, a separate ban store keyed on the onion hostname (or the full multiaddr prefix) should be maintained alongside the existing IP-based `BanList`. `is_addr_banned` must be extended to check this store. The same extension must apply to the `accept_peer` inbound check. Alternatively, if Tor support is not intended for production, Onion3 addresses should be rejected at the `add_addr` boundary.

## Proof of Concept
```rust
let mut peer_store = PeerStore::default();
let onion_addr: Multiaddr =
    "/onion3/vww6ybal4bd7szmgncyruucpgfkqahzddi37ktceo3ah7ngmcopnpyyd:1234"
        .parse().unwrap();
peer_store.add_addr(onion_addr.clone(), Flags::COMPATIBILITY).unwrap();

// Simulate score-based ban
peer_store.ban_addr(&onion_addr, 600_000, "misbehaviour".into());

// Ban list is empty — peer was never banned
assert_eq!(peer_store.ban_list().count(), 0);
// is_addr_banned returns false — inbound reconnection not blocked
assert!(!peer_store.is_addr_banned(&onion_addr));
// Peer can be re-added immediately via discovery
peer_store.add_addr(onion_addr.clone(), Flags::COMPATIBILITY).unwrap();
assert_eq!(peer_store.addr_manager().count(), 1); // back in the pool
```

The existing test suite in `network/src/tests/peer_store.rs` already tests the ban path for IPv6 addresses (`test_report` at L53–73) and confirms the ban works for IP-based addresses. An analogous test with an Onion3 multiaddr would demonstrate the bypass.
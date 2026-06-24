Audit Report

## Title
Port-Zero Address Injection via `GetNodes.listen_port` Bypasses Port Validation — (`network/src/protocols/discovery/mod.rs`, `network/src/protocols/discovery/state.rs`)

## Summary
Any unprivileged inbound peer can send a `GetNodes` message with `listen_port: Some(0)`. The handler calls `update_port(0)` with no port validation, transitions `RemoteAddress` to `Listen` state with a `TCP/0` multiaddr, and immediately writes it to the peer store. The sole filter — `is_valid_addr` — checks only IP reachability, never the port value. The injected address is then eligible for outbound dial attempts via `fetch_addrs_to_attempt` and is announced to up to three other connected peers on the first `notify` tick (within 60 seconds).

## Finding Description

**Root cause — `update_port` accepts port 0:**
`state.rs:153–167` maps every `Protocol::Tcp(_)` component to `Protocol::Tcp(port)` with no guard on `port == 0`. The result is stored as `RemoteAddress::Listen(addr)`.

**Immediate peer store write — `add_new_addr` path:**
`mod.rs:124–134` calls `self.addr_mgr.add_new_addr(session.id, (addr.clone(), flags))` immediately after `update_port`. `add_new_addrs` (`mod.rs:352–362`) filters only through `is_valid_addr`, which at `mod.rs:332–341` calls `is_reachable(socket_addr.ip())` — the port is never inspected. `peer_store_impl.rs:71–79` stores the entry with `last_connected_at_ms = 0`.

**Outbound dial path — `fetch_addrs_to_attempt`:**
`outbound_peer.rs:123–132` calls `peer_store.fetch_addrs_to_attempt(...)`. The filter at `peer_store_impl.rs:230–239` requires only that the address is not currently connected and was not tried in the last minute — it does **not** require `last_connected_at_ms > 0`. A freshly injected TCP/0 entry passes all conditions and will be dialed, failing immediately and incrementing `attempts_count`.

**Announce relay path:**
`mod.rs:231–237` calls `state.check_timer(now, ANNOUNCE_INTERVAL)`. Because `last_announce` is `None` on a new session, `check_timer` (`state.rs:94–109`) returns `Some(addr)` on the very first `notify` tick (fired within 60 seconds). The only filter is again `is_valid_addr` (IP-only). If `node_flags` returns `Some` (i.e., identify has completed, which is typical), the TCP/0 address is pushed to `announce_list` and forwarded to up to three other sessions (`mod.rs:240–261`). Receiving nodes process it through the `Nodes` handler, which calls `add_new_addrs` — again filtered only by `is_valid_addr` — storing the TCP/0 entry in their own peer stores.

**Existing guards are insufficient:**
- `is_valid_addr`: IP reachability only, port ignored.
- `add_addr` ban-list check: only checks IP bans, not port validity.
- `fetch_random_addrs` (used for discovery relay): does require `last_connected_at_ms > 0`, so this path is correctly blocked. However, `fetch_addrs_to_attempt` (used for actual outbound connections) has no such requirement.

## Impact Explanation
This matches **Low (501–2000 points): Any other important performance improvements for CKB.** The attack pollutes the peer stores of reachable nodes with undiallable TCP/0 entries, causes wasted outbound connection attempts (each failing immediately, incrementing `attempts_count` toward `ADDR_MAX_RETRIES`), and propagates the invalid entry to additional peers via the announce mechanism. The damage is self-limiting: after `ADDR_MAX_RETRIES` failures with `last_connected_at_ms == 0`, `is_connectable` returns `false` and the entry is eventually purged. The attack does not crash nodes, cause consensus deviation, or damage the economy, and does not rise to the level of network congestion.

## Likelihood Explanation
Trivially triggerable by any peer that can establish a TCP connection and open the discovery protocol. No authentication, proof-of-work, or special role is required. The `GetNodes` message is the standard first message sent on outbound sessions, so the handler is always reachable on inbound sessions. The attack is repeatable from any IP.

## Recommendation
Add a port validity check in `update_port` before accepting the value:

```rust
// network/src/protocols/discovery/state.rs
pub(crate) fn update_port(&mut self, port: u16) {
    if port == 0 {
        return; // reject port-zero
    }
    // ... existing logic
}
```

Alternatively, extend `is_valid_addr` to also reject port zero:

```rust
// network/src/protocols/discovery/mod.rs
fn is_valid_addr(&self, addr: &Multiaddr) -> bool {
    match multiaddr_to_socketaddr(addr) {
        Some(socket_addr) => {
            socket_addr.port() != 0
                && (self.discovery_local_address || is_reachable(socket_addr.ip()))
        }
        None => true,
    }
}
```

The `update_port` guard is preferable as it stops the invalid address from ever entering the `RemoteAddress::Listen` state.

## Proof of Concept
1. Establish a TCP connection to a CKB node (become an inbound peer from the node's perspective).
2. Complete the p2p handshake and open the discovery protocol substream.
3. Send `DiscoveryMessage::GetNodes { listen_port: Some(0), count: 1000, version: 0, required_flags: Flags::COMPATIBILITY }`.
4. **Assert (immediate):** query the victim's peer store; it contains an `AddrInfo` entry whose `addr` has `Protocol::Tcp(0)` for the attacker's IP, with `last_connected_at_ms = 0`.
5. **Assert (within 60 seconds):** observe the victim send a `Nodes(announce=true)` message to other connected peers containing the TCP/0 multiaddr.
6. **Assert (connection attempt):** within the next `try_connect_interval` tick of `OutboundPeerService`, the victim calls `dial_identify` or `dial_feeler` targeting the TCP/0 address, which fails immediately; `attempts_count` on the peer store entry increments.
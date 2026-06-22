Looking at the exact code paths:

### Title
Onion3 (Tor) Peers Evade All Score-Based and Identify-Triggered Bans — (`network/src/peer_store/peer_store_impl.rs`, `network/src/peer_store/ban_list.rs`)

---

### Summary

`ban_addr` and `is_addr_banned` both delegate to `multiaddr_to_socketaddr`, which returns `None` for Onion3 multiaddrs. As a result, calling `ban_addr` on a Tor peer silently skips the `ban_list` insertion while still removing the peer from `addr_manager`. Because `is_addr_banned` also returns `false` for every Onion3 address, the peer is never blocked from reconnecting. This breaks the invariant that a banned peer must be prevented from reconnecting regardless of transport type.

---

### Finding Description

**`ban_addr` skips `ban_list` for Onion3 addresses** [1](#0-0) 

`multiaddr_to_socketaddr` returns `None` for `/onion3/…` multiaddrs, so the `if let Some(addr)` branch is never entered and `ban_network` is never called. The `addr_manager.remove(addr)` call on line 291 still executes unconditionally, removing the peer from the known-good address set, but nothing is written to `ban_list`.

**`is_addr_banned` always returns `false` for Onion3 addresses** [2](#0-1) 

`multiaddr_to_socketaddr` returns `None` → `.map(…)` produces `None` → `unwrap_or_default()` returns `false`. Every ban check for an Onion3 peer passes.

**`accept_peer` relies on `is_addr_banned` as the sole reconnection gate** [3](#0-2) 

Because `is_addr_banned` always returns `false` for Onion3, the `PeerError::Banned` branch is never reached for Tor peers.

**`ban_session` (the production ban entry point) calls `ban_addr` with the peer's connected addr** [4](#0-3) 

For a Tor-connected peer, `peer.connected_addr` is an Onion3 multiaddr, so the entire ban is a no-op in `ban_list`.

**Onion3 is a supported, production-enabled transport** [5](#0-4) 

The `listen_on_onion` config option and `OnionService` are production code, not test stubs. `addr_manager.fetch_random` explicitly special-cases Onion3 to keep such peers connectable: [6](#0-5) 

---

### Impact Explanation

Any peer connecting over Tor that triggers a ban (wrong network identifier via the Identify protocol, or score-based misbehavior via `report`) will:

1. Be disconnected from the current session.
2. Have its Onion3 address removed from `addr_manager` (so the local node won't dial it outbound again).
3. **Not** be recorded in `ban_list`.
4. Be able to reconnect immediately as an inbound peer, passing the `is_addr_banned` check every time.

This completely nullifies score-based banning and identify-triggered banning for all Tor-connected peers. A misbehaving Tor peer can cycle through reconnections indefinitely, consuming inbound connection slots and bypassing all misbehavior enforcement.

---

### Likelihood Explanation

- Onion3 support is an opt-in but documented production feature (`listen_on_onion = true`).
- The attacker needs only a Tor client and knowledge of the target node's onion address — no privileged access, no hashpower, no key material.
- The bug is triggered by the normal ban code path; no special message crafting is required beyond whatever behavior normally triggers a ban (e.g., sending an identify message with a wrong network name).

---

### Recommendation

The ban system must be extended to handle non-IP transports. Options:

1. **Onion3-keyed ban list**: Store banned Onion3 host bytes (the 35-byte public key) in a separate set alongside the IP-keyed `HashMap<IpNetwork, BannedAddr>`. Update `is_addr_banned` and `ban_addr` to check/insert this set when `multiaddr_to_socketaddr` returns `None` and the address contains an `Onion3` component.
2. **PeerId-based banning**: Since every connected peer has a `PeerId`, maintain a `HashSet<PeerId>` of banned peer IDs and check it in `accept_peer` before the `is_addr_banned` call.

Either approach must be applied consistently in both `ban_addr` (write path) and `is_addr_banned` / `accept_peer` (read/enforcement path).

---

### Proof of Concept

```rust
use ckb_network::{
    Flags, peer_store::{PeerStore, Behaviour},
    multiaddr::Multiaddr,
};

let mut peer_store = PeerStore::default();
let onion_addr: Multiaddr =
    "/onion3/vww6ybal4bd7szmgncyruucpgfkqahzddi37ktceo3ah7ngmcopnpyyd:1234/p2p/QmXXX"
        .parse().unwrap();

// Add the Onion3 peer to addr_manager (simulates discovery/outbound add)
peer_store.add_addr(onion_addr.clone(), Flags::all()).unwrap();

// Trigger a ban (as ban_session would do)
peer_store.ban_addr(&onion_addr, 3_600_000, "misbehavior".into());

// Invariant: ban_list must contain the peer
assert!(peer_store.ban_list().count() > 0);          // FAILS: count == 0
assert!(peer_store.is_addr_banned(&onion_addr));     // FAILS: returns false
// The peer can reconnect freely.
```

### Citations

**File:** network/src/peer_store/peer_store_impl.rs (L286-292)
```rust
    pub(crate) fn ban_addr(&mut self, addr: &Multiaddr, timeout_ms: u64, ban_reason: String) {
        if let Some(addr) = multiaddr_to_socketaddr(addr) {
            let network = ip_to_network(addr.ip());
            self.ban_network(network, timeout_ms, ban_reason)
        }
        self.addr_manager.remove(addr);
    }
```

**File:** network/src/peer_store/ban_list.rs (L68-72)
```rust
    pub fn is_addr_banned(&self, addr: &Multiaddr) -> bool {
        multiaddr_to_socketaddr(addr)
            .map(|socket_addr| self.is_ip_banned(&socket_addr.ip()))
            .unwrap_or_default()
    }
```

**File:** network/src/peer_registry.rs (L109-111)
```rust
            if peer_store.is_addr_banned(&remote_addr) {
                return Err(PeerError::Banned.into());
            }
```

**File:** network/src/network.rs (L264-268)
```rust
                self.peer_store.lock().ban_addr(
                    &peer.connected_addr,
                    duration.as_millis() as u64,
                    reason,
                );
```

**File:** util/onion/src/onion_service.rs (L41-50)
```rust
        let onion_multi_addr_str = format!(
            "/onion3/{}:{}/p2p/{}",
            tor_address_without_dot_onion, onion_external_port, node_id
        );
        let onion_multi_addr = Multiaddr::from_str(&onion_multi_addr_str).map_err(|err| {
            InternalErrorKind::Other.other(format!(
                "Failed to parse onion address {} to multi_addr: {:?}",
                onion_multi_addr_str, err
            ))
        })?;
```

**File:** network/src/peer_store/addr_manager.rs (L74-82)
```rust
                None => {
                    if filter(&addr_info) {
                        if addr_info.is_connectable(now_ms)
                            || addr_info
                                .addr
                                .iter()
                                .any(|p| matches!(p, Protocol::Onion3(_)))
                        {
                            addr_infos.push(addr_info);
```

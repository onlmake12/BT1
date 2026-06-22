The code path is concrete and traceable. Here is the full analysis:

---

### Title
Inbound Peer Ephemeral Address Promoted to Listen and Broadcast via `change_to_listen()` — (`network/src/protocols/discovery/state.rs`, `network/src/protocols/discovery/mod.rs`)

---

### Summary

An unprivileged inbound peer that completes the identify handshake and then sends `GetNodes{version: 1, listen_port: None}` causes its ephemeral TCP source address to be unconditionally promoted from `RemoteAddress::Init` to `RemoteAddress::Listen`. The `notify()` tick then announces that address to up to 3 random peers every 24 hours. The identify protocol's own source comment explicitly documents that inbound addresses must not be broadcast because they are unverified — yet the discovery protocol's `change_to_listen()` path bypasses that protection entirely.

---

### Finding Description

**Step 1 — Inbound connection initializes `Init` state.**

When an inbound session is established, `SessionState::new()` sets:

```rust
RemoteAddress::Init(context.session.address.clone())
```

`context.session.address` is the ephemeral TCP source address of the attacker (e.g., `1.2.3.4:54321`). [1](#0-0) 

**Step 2 — Attacker sends `GetNodes{version: 1, listen_port: None}`.**

In `received()`, the `listen_port: None` branch is skipped entirely. Then, unconditionally:

```rust
if version >= state::REUSE_PORT_VERSION {
    state.remote_addr.change_to_listen();
}
``` [2](#0-1) 

**Step 3 — `change_to_listen()` promotes the ephemeral address.**

```rust
pub(crate) fn change_to_listen(&mut self) {
    if let RemoteAddress::Init(addr) = self {
        *self = RemoteAddress::Listen(addr.clone());
    }
}
```

No port validation, no listen verification. `Init(1.2.3.4:54321)` becomes `Listen(1.2.3.4:54321)`. [3](#0-2) 

**Step 4 — `check_timer()` returns the promoted address.**

```rust
if let RemoteAddress::Listen(addr) = &self.remote_addr {
    Some(addr)
} else {
    None
}
``` [4](#0-3) 

**Step 5 — Two gates in `notify()` that the attacker can pass.**

```rust
if let Some(addr) = state
    .check_timer(now, ANNOUNCE_INTERVAL)
    .filter(|addr| self.addr_mgr.is_valid_addr(addr))
    && let Some(flags) = self.addr_mgr.node_flags(*id)
{
    announce_list.push((addr.clone(), flags));
}
``` [5](#0-4) 

- **`is_valid_addr()`**: passes for any public (routable) IP. An attacker connecting from a public IP clears this. [6](#0-5) 

- **`node_flags()`**: requires `peer.identify_info` to be `Some`. This is set when the identify protocol completes. Critically, `identify_info` is set for **both** inbound and outbound sessions — the `is_outbound()` guard at line 415 only gates `add_outbound_addr`, not the `identify_info` assignment itself. [7](#0-6) 

**Step 6 — Ephemeral address broadcast to 3 peers.**

The address is pushed into `announce_multiaddrs` of up to 3 randomly chosen sessions and sent as `Nodes{announce: true}`. Receiving peers call `add_new_addrs()` → `peer_store.add_addr()`, storing the bogus address. [8](#0-7) 

**Step 7 — The identify protocol's own comment documents the violated invariant.**

```rust
if context.session.ty.is_outbound() {
    // why don't set inbound here?
    // because inbound address can't feeler during staying connected
    // and if set it to peer store, it will be broadcast to the entire network,
    // but this is an unverified address
``` [9](#0-8) 

The identify protocol explicitly refuses to add inbound addresses to the peer store for exactly this reason. The discovery protocol's `change_to_listen()` path violates that same invariant.

---

### Impact Explanation

An attacker with a public IP who completes the identify handshake (a normal part of the P2P protocol) can cause their ephemeral TCP source port to be stored as a "listen address" in the peer stores of up to 3 peers per 24-hour cycle per connection. Those peers will attempt to connect to the bogus address, fail, and eventually prune it — but the address is also re-announced by those peers' own discovery sessions if it ends up in their `announce_multiaddrs`. The practical impact is peer store pollution and wasted connection attempts, not network-wide propagation. The claimed "Critical" severity is overstated; this is a **Medium** severity issue.

---

### Likelihood Explanation

Exploitable by any peer that can establish an inbound connection and complete the identify handshake — both are standard, unprivileged operations. No special knowledge or timing is required. The `version` field in `GetNodes` is attacker-controlled with no server-side verification.

---

### Recommendation

In `received()`, the `change_to_listen()` call should only be made when `listen_port` was explicitly provided and `update_port()` has already promoted the address to `Listen`. Since `update_port()` already transitions `Init → Listen` when a port is given, the unconditional `change_to_listen()` block at lines 136–139 should be removed or gated on `listen_port.is_some()`. This aligns with the design intent documented in the identify protocol. [10](#0-9) 

---

### Proof of Concept

```
1. Attacker (public IP 1.2.3.4, ephemeral port 54321) connects inbound to victim node.
2. Identify handshake completes → peer.identify_info = Some(PeerIdentifyInfo{flags: ...}).
3. Attacker sends: DiscoveryMessage::GetNodes { version: 1, listen_port: None, count: 1, required_flags: ... }
4. In received(): listen_port is None → update_port() skipped.
   version (1) >= REUSE_PORT_VERSION (1) → change_to_listen() called.
   remote_addr: Init(1.2.3.4:54321) → Listen(1.2.3.4:54321).
5. After ANNOUNCE_INTERVAL (24h), notify() fires:
   check_timer() → Some(1.2.3.4:54321)
   is_valid_addr(1.2.3.4:54321) → true (public IP)
   node_flags(session_id) → Some(flags) (identify completed)
   announce_list = [(1.2.3.4:54321, flags)]
6. Up to 3 random peer sessions receive Nodes{announce:true, items:[1.2.3.4:54321]}.
7. Those peers call add_new_addrs() → peer_store.add_addr(1.2.3.4:54321, flags).
8. Bogus ephemeral address is now stored in peer stores of 3 nodes.
```

### Citations

**File:** network/src/protocols/discovery/state.rs (L80-82)
```rust
        } else {
            RemoteAddress::Init(context.session.address.clone())
        };
```

**File:** network/src/protocols/discovery/state.rs (L101-105)
```rust
            if let RemoteAddress::Listen(addr) = &self.remote_addr {
                Some(addr)
            } else {
                None
            }
```

**File:** network/src/protocols/discovery/state.rs (L147-151)
```rust
    pub(crate) fn change_to_listen(&mut self) {
        if let RemoteAddress::Init(addr) = self {
            *self = RemoteAddress::Listen(addr.clone());
        }
    }
```

**File:** network/src/protocols/discovery/mod.rs (L124-139)
```rust
                            if let Some(port) = listen_port {
                                state.remote_addr.update_port(port);
                                state.addr_known.insert(state.remote_addr.to_inner());
                                // add client listen address to manager
                                if let RemoteAddress::Listen(ref addr) = state.remote_addr {
                                    let flags = self.addr_mgr.node_flags(session.id);
                                    self.addr_mgr.add_new_addr(
                                        session.id,
                                        (addr.clone(), flags.unwrap_or(Flags::COMPATIBILITY)),
                                    );
                                }
                            }
                            if version >= state::REUSE_PORT_VERSION {
                                // after enable reuse port, it can be broadcast
                                state.remote_addr.change_to_listen();
                            }
```

**File:** network/src/protocols/discovery/mod.rs (L231-237)
```rust
            if let Some(addr) = state
                .check_timer(now, ANNOUNCE_INTERVAL)
                .filter(|addr| self.addr_mgr.is_valid_addr(addr))
                && let Some(flags) = self.addr_mgr.node_flags(*id)
            {
                announce_list.push((addr.clone(), flags));
            }
```

**File:** network/src/protocols/discovery/mod.rs (L243-261)
```rust
            for announce_multiaddr in announce_list {
                keys.shuffle(&mut rng);
                for key in keys.iter().take(3) {
                    if let Some(value) = self.sessions.get_mut(key) {
                        trace!(
                            ">> send {:?} to: {:?}, containing: {}",
                            announce_multiaddr,
                            value.remote_addr,
                            value.addr_known.contains(&announce_multiaddr)
                        );
                        if value.announce_multiaddrs.len() < ANNOUNCE_THRESHOLD
                            && !value.addr_known.contains(&announce_multiaddr)
                        {
                            value.announce_multiaddrs.push(announce_multiaddr.clone());
                            value.addr_known.insert(&announce_multiaddr);
                        }
                    }
                }
            }
```

**File:** network/src/protocols/discovery/mod.rs (L332-341)
```rust
    fn is_valid_addr(&self, addr: &Multiaddr) -> bool {
        if !self.discovery_local_address {
            match multiaddr_to_socketaddr(addr) {
                Some(socket_addr) => is_reachable(socket_addr.ip()),
                None => true,
            }
        } else {
            true
        }
    }
```

**File:** network/src/protocols/identify/mod.rs (L399-411)
```rust
            Some((flags, client_version)) => {
                let registry_client_version = |version: String| {
                    self.network_state.with_peer_registry_mut(|registry| {
                        if let Some(peer) = registry.get_peer_mut(context.session.id) {
                            peer.identify_info = Some(PeerIdentifyInfo {
                                client_version: version,
                                flags,
                            })
                        }
                    });
                };

                registry_client_version(client_version);
```

**File:** network/src/protocols/identify/mod.rs (L415-423)
```rust
                if context.session.ty.is_outbound() {
                    // why don't set inbound here?
                    // because inbound address can't feeler during staying connected
                    // and if set it to peer store, it will be broadcast to the entire network,
                    // but this is an unverified address

                    self.network_state.with_peer_store_mut(|peer_store| {
                        peer_store.add_outbound_addr(context.session.address.clone(), flags);
                    });
```

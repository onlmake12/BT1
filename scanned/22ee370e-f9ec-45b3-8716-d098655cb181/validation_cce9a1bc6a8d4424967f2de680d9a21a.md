The code confirms the claims. Let me verify the key logic paths precisely.

**Inbound init** (`state.rs` line 81): inbound sessions start as `RemoteAddress::Init(ephemeral_addr)`. [1](#0-0) 

**Independent `if` blocks** (`mod.rs` lines 124–139): `update_port` is gated on `listen_port.is_some()`, but `change_to_listen()` is gated only on `version >= REUSE_PORT_VERSION` — the two conditions are completely independent. [2](#0-1) 

**`change_to_listen()`** (`state.rs` lines 147–151): unconditionally promotes `Init(addr)` → `Listen(addr)` with no port validation. [3](#0-2) 

**`check_timer`** (`state.rs` lines 94–108): fires on the very first tick because `last_announce` starts as `None` (`unwrap_or(true)`), and returns the address only when `remote_addr` is `Listen` — which it now is after the promotion. [4](#0-3) 

**`is_valid_addr`** (`mod.rs` lines 332–341): only checks IP reachability (`is_reachable`), not whether the port is a valid listen port. [5](#0-4) 

**`node_flags`** (`mod.rs` lines 397–402): requires `identify_info`, satisfied by any peer completing the normal P2P handshake. [6](#0-5) 

**Broadcast** (`mod.rs` lines 243–258): the ephemeral address is pushed into `announce_multiaddrs` of up to 3 sessions and sent as `Nodes{announce: true}`. [7](#0-6) 

All claims check out against the actual code. The vulnerability is real.

---

Audit Report

## Title
Inbound Peer Promotes Ephemeral Address to Listen via `change_to_listen` Without Valid `listen_port` — (`network/src/protocols/discovery/mod.rs`)

## Summary
The `GetNodes` handler in the discovery protocol contains two independent `if` blocks: one that updates the remote port (gated on `listen_port.is_some()`) and one that promotes the address to `RemoteAddress::Listen` (gated only on `version >= REUSE_PORT_VERSION`). An inbound peer sending `version=1, listen_port=None` bypasses the port update but still triggers `change_to_listen()`, promoting its ephemeral TCP source port to a declared listen address. Within 60 seconds (first notify tick), this bogus address is gossiped to up to 3 peers as a confirmed listen address, poisoning their peer stores.

## Finding Description
**Root cause:** `mod.rs` lines 124–139 contain two independent `if` blocks. The first (`if let Some(port) = listen_port`) updates the port and adds the address to the addr manager. The second (`if version >= REUSE_PORT_VERSION`) calls `state.remote_addr.change_to_listen()` unconditionally — it does not check whether `listen_port` was provided. The design intent of `REUSE_PORT_VERSION` is that when `SO_REUSEPORT` is active, the source port equals the listen port, making promotion safe. However, the code does not enforce that `listen_port` was actually supplied before promoting.

**Exploit flow:**
1. Attacker opens inbound TCP connection from public IP `1.2.3.4:54321`. `state.rs` line 81 sets `remote_addr = RemoteAddress::Init(1.2.3.4:54321)`.
2. Normal P2P identify handshake completes; `identify_info` is set, so `node_flags()` returns `Some(flags)`.
3. Attacker sends `GetNodes { version: 1, listen_port: None, count: 1000, required_flags: 0 }`.
4. Handler: `listen_port=None` → `update_port` skipped. `version=1 >= REUSE_PORT_VERSION` → `change_to_listen()` called. `remote_addr` transitions: `Init(1.2.3.4:54321)` → `Listen(1.2.3.4:54321)`.
5. Within 60 seconds, `notify` fires. `check_timer` returns `Some(1.2.3.4:54321)` because `last_announce` is `None` (`unwrap_or(true)` at `state.rs` line 98). `is_valid_addr` passes (public IP). `node_flags` returns `Some`. Address enters `announce_list`.
6. Up to 3 other sessions receive `Nodes{announce: true, items: [1.2.3.4:54321]}`.
7. Recipients call `add_new_addrs` → `1.2.3.4:54321` persisted in peer store via `peer_store.add_addr`.
8. Recipients attempt outbound connections to the ephemeral port → connection refused → slot wasted, peer store polluted.

**Why existing guards fail:** `is_valid_addr` only checks `is_reachable(ip)` — it does not validate the port. `node_flags` is satisfied by any peer completing the standard handshake. There is no guard that verifies `remote_addr` was explicitly declared as a listen address before `check_timer` returns it.

## Impact Explanation
This is a **High** severity issue matching: *"Vulnerabilities or bad designs which could cause CKB network congestion with few costs."* An attacker with multiple simultaneous inbound connections can continuously poison the peer stores of reachable nodes. Each poisoned node wastes outbound connection slots attempting to reach ephemeral ports, degrades its peer discovery quality, and re-propagates the bogus addresses to further peers. At scale, this degrades the network's ability to maintain valid peer connections, constituting persistent low-cost network connectivity degradation.

## Likelihood Explanation
Exploitation requires only: (1) establishing an inbound TCP connection (no special privileges), (2) completing the standard identify protocol handshake (normal P2P behavior), and (3) sending one crafted `GetNodes` message. The first gossip fires within 60 seconds (first `ANNOUNCE_CHECK_INTERVAL` tick). The attack is repeatable with multiple connections and requires no keys, hashpower, or elevated access.

## Recommendation
Gate `change_to_listen()` on `listen_port` being explicitly provided. The two `if` blocks must not be independent:

```rust
if let Some(port) = listen_port {
    state.remote_addr.update_port(port);
    state.addr_known.insert(state.remote_addr.to_inner());
    if let RemoteAddress::Listen(ref addr) = state.remote_addr {
        let flags = self.addr_mgr.node_flags(session.id);
        self.addr_mgr.add_new_addr(
            session.id,
            (addr.clone(), flags.unwrap_or(Flags::COMPATIBILITY)),
        );
    }
    if version >= state::REUSE_PORT_VERSION {
        // Only promote when listen_port was explicitly declared
        state.remote_addr.change_to_listen();
    }
}
// Remove the unconditional change_to_listen() block below
```

Alternatively, add an explicit guard: `if version >= state::REUSE_PORT_VERSION && listen_port.is_some()`.

## Proof of Concept
**Manual steps:**
1. Connect to a victim node from a public IP (e.g., `1.2.3.4`) on an ephemeral port (e.g., `54321`).
2. Complete the identify protocol handshake normally.
3. Send a `GetNodes` message with `version=1`, `listen_port=None`, `count=1000`, `required_flags=0`.
4. Wait up to 60 seconds for the first `notify` tick.
5. Observe that up to 3 connected peers of the victim receive a `Nodes{announce: true}` message containing `1.2.3.4:54321`.
6. Inspect those peers' peer stores to confirm `1.2.3.4:54321` was added via `add_new_addrs`.
7. Confirm that connection attempts to `1.2.3.4:54321` fail (port not listening).

**Unit test plan:** In `mod.rs` tests, create an inbound `SessionState`, send `GetNodes { version: REUSE_PORT_VERSION, listen_port: None, ... }`, assert that `state.remote_addr` remains `RemoteAddress::Init` (not promoted to `Listen`), and assert that `check_timer` returns `None` for that session.

### Citations

**File:** network/src/protocols/discovery/state.rs (L80-82)
```rust
        } else {
            RemoteAddress::Init(context.session.address.clone())
        };
```

**File:** network/src/protocols/discovery/state.rs (L94-109)
```rust
    pub(crate) fn check_timer(&mut self, now: Instant, interval: Duration) -> Option<&Multiaddr> {
        if self
            .last_announce
            .map(|time| now.saturating_duration_since(time) > interval)
            .unwrap_or(true)
        {
            self.last_announce = Some(now);
            if let RemoteAddress::Listen(addr) = &self.remote_addr {
                Some(addr)
            } else {
                None
            }
        } else {
            None
        }
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

**File:** network/src/protocols/discovery/mod.rs (L243-258)
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

**File:** network/src/protocols/discovery/mod.rs (L397-402)
```rust
    fn node_flags(&self, id: SessionId) -> Option<Flags> {
        self.network_state.with_peer_registry(|reg| {
            reg.get_peer(id)
                .and_then(|peer| peer.identify_info.as_ref().map(|a| a.flags))
        })
    }
```

### Title
Port-Zero Address Injection via `GetNodes.listen_port` Bypasses `is_valid_addr` Port Check — (`network/src/protocols/discovery/mod.rs`, `network/src/protocols/discovery/state.rs`)

---

### Summary

An unprivileged remote peer can connect to a CKB node and send a `GetNodes` message with `listen_port: Some(0)`. The handler calls `update_port(0)` with no port validation, stores a multiaddr containing `TCP/0` in the peer store, and then announces that address to up to three other connected peers within 60 seconds — all because `is_valid_addr` only checks IP reachability, never the port value.

---

### Finding Description

**Step 1 — Inbound session state is `RemoteAddress::Init`.**

When a remote peer connects inbound, `SessionState::new` sets:

```rust
RemoteAddress::Init(context.session.address.clone())
``` [1](#0-0) 

**Step 2 — `GetNodes` handler calls `update_port` with the attacker-supplied value.**

In the `received` handler, if `listen_port` is `Some(port)`, `update_port(port)` is called unconditionally:

```rust
if let Some(port) = listen_port {
    state.remote_addr.update_port(port);
    ...
    if let RemoteAddress::Listen(ref addr) = state.remote_addr {
        self.addr_mgr.add_new_addr(session.id, (addr.clone(), ...));
    }
}
``` [2](#0-1) 

**Step 3 — `update_port` accepts port 0 without validation.**

```rust
pub(crate) fn update_port(&mut self, port: u16) {
    if let RemoteAddress::Init(addr) = self {
        let addr = addr.into_iter().map(|proto| {
            match proto {
                Protocol::Tcp(_) => Protocol::Tcp(port),  // port=0 accepted
                value => value,
            }
        }).collect();
        *self = RemoteAddress::Listen(addr);
    }
}
``` [3](#0-2) 

**Step 4 — `is_valid_addr` only checks IP reachability, not port.**

```rust
fn is_valid_addr(&self, addr: &Multiaddr) -> bool {
    if !self.discovery_local_address {
        match multiaddr_to_socketaddr(addr) {
            Some(socket_addr) => is_reachable(socket_addr.ip()),  // port ignored
            None => true,
        }
    } else {
        true
    }
}
``` [4](#0-3) 

`add_new_addrs` uses this as its sole filter before writing to the peer store:

```rust
for (addr, flags) in addrs.into_iter().filter(|addr| self.is_valid_addr(&addr.0)) {
    peer_store.add_addr(addr.clone(), flags)?;
}
``` [5](#0-4) 

**Step 5 — The port-zero address is announced to other peers within 60 seconds.**

The `notify` loop (fired every `ANNOUNCE_CHECK_INTERVAL = 60s`) calls `check_timer`, which returns `Some(addr)` on the first call (since `last_announce` is `None`). It then re-applies only `is_valid_addr` (IP-only check) before pushing to `announce_list` and sending to up to 3 other sessions:

```rust
if let Some(addr) = state.check_timer(now, ANNOUNCE_INTERVAL)
    .filter(|addr| self.addr_mgr.is_valid_addr(addr))
    && let Some(flags) = self.addr_mgr.node_flags(*id)
{
    announce_list.push((addr.clone(), flags));
}
``` [6](#0-5) 

---

### Impact Explanation

- **Peer store pollution**: Every node that processes the injected `GetNodes` stores a `TCP/0` address. [7](#0-6) 
- **Active relay**: The port-zero address is announced to up to 3 other connected peers within 60 seconds, propagating the invalid entry across the discovery graph. [8](#0-7) 
- **Wasted connection attempts**: Nodes that receive the address via announce will store it and may attempt to dial `TCP/0`, which always fails, incrementing `attempts_count` and eventually marking the slot as non-connectable. [9](#0-8) 
- **Note**: The `fetch_random_addrs` relay path is NOT affected — it requires `last_connected_at_ms > 0`, which a freshly-added address never has. The announce path is the confirmed relay vector. [10](#0-9) 

---

### Likelihood Explanation

Any unprivileged peer that can establish a TCP connection to a CKB node can trigger this. No authentication, no PoW, no special role required. The `GetNodes` message is a standard first message sent by outbound peers, so the handler is always reachable on inbound sessions.

---

### Recommendation

Add a port validity check in `update_port` or at the `add_new_addrs` entry point:

```rust
// In update_port or add_new_addrs:
if port == 0 {
    return; // reject port-zero
}
```

Alternatively, extend `is_valid_addr` to also validate that the TCP port is non-zero:

```rust
fn is_valid_addr(&self, addr: &Multiaddr) -> bool {
    match multiaddr_to_socketaddr(addr) {
        Some(socket_addr) => socket_addr.port() != 0 && is_reachable(socket_addr.ip()),
        None => true,
    }
}
``` [4](#0-3) 

---

### Proof of Concept

1. Establish a TCP connection to a CKB node (become an inbound peer from the node's perspective).
2. Complete the p2p handshake and open the discovery protocol.
3. Send `DiscoveryMessage::GetNodes { listen_port: Some(0), count: 1000, version: 0, required_flags: ... }`.
4. Assert: the victim's peer store now contains an entry with `TCP/0` for the attacker's IP.
5. Wait up to 60 seconds; observe that the victim sends a `Nodes(announce=true)` message to other connected peers containing the `TCP/0` address.

### Citations

**File:** network/src/protocols/discovery/state.rs (L80-82)
```rust
        } else {
            RemoteAddress::Init(context.session.address.clone())
        };
```

**File:** network/src/protocols/discovery/state.rs (L153-167)
```rust
    pub(crate) fn update_port(&mut self, port: u16) {
        if let RemoteAddress::Init(addr) = self {
            let addr = addr
                .into_iter()
                .map(|proto| {
                    match proto {
                        // TODO: other transport, UDP for example
                        Protocol::Tcp(_) => Protocol::Tcp(port),
                        value => value,
                    }
                })
                .collect();
            *self = RemoteAddress::Listen(addr);
        }
    }
```

**File:** network/src/protocols/discovery/mod.rs (L124-134)
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

**File:** network/src/protocols/discovery/mod.rs (L240-261)
```rust
        if !announce_list.is_empty() {
            let mut rng = rand::thread_rng();
            let mut keys = self.sessions.keys().cloned().collect::<Vec<_>>();
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

**File:** network/src/protocols/discovery/mod.rs (L352-362)
```rust
        for (addr, flags) in addrs.into_iter().filter(|addr| self.is_valid_addr(&addr.0)) {
            trace!("Add discovered address:{:?}", addr);
            self.network_state.with_peer_store_mut(|peer_store| {
                if let Err(err) = peer_store.add_addr(addr.clone(), flags) {
                    debug!(
                        "Failed to add discovered address to peer_store {:?} {:?}",
                        err, addr
                    );
                }
            });
        }
```

**File:** network/src/peer_store/peer_store_impl.rs (L71-79)
```rust
    pub fn add_addr(&mut self, addr: Multiaddr, flags: Flags) -> Result<()> {
        if self.ban_list.is_addr_banned(&addr) {
            return Ok(());
        }
        self.check_purge()?;
        let score = self.score_config.default_score;
        self.addr_manager
            .add(AddrInfo::new(addr, 0, score, flags.bits()));
        Ok(())
```

**File:** network/src/peer_store/peer_store_impl.rs (L276-282)
```rust
        let filter = |peer_addr: &AddrInfo| {
            required_flags_filter(required_flags, Flags::from_bits_truncate(peer_addr.flags))
                && peer_addr.connected(|t| t > addr_expired_ms)
        };

        // get success connected addrs.
        self.addr_manager.fetch_random(count, filter)
```

**File:** network/src/peer_store/types.rs (L89-105)
```rust
    pub fn is_connectable(&self, now_ms: u64) -> bool {
        // do not remove addr tried in last minute
        if self.tried_in_last_minute(now_ms) {
            return true;
        }
        // we give up if never connect to this addr
        if self.last_connected_at_ms == 0 && self.attempts_count >= ADDR_MAX_RETRIES {
            return false;
        }
        // consider addr is not connectable if failed too many times
        if now_ms.saturating_sub(self.last_connected_at_ms) > ADDR_TIMEOUT_MS
            && (self.attempts_count >= ADDR_MAX_FAILURES)
        {
            return false;
        }
        true
    }
```

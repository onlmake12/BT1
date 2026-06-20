The code path is real and traceable. Let me walk through each step precisely.

**Step 1 — Inbound session initialization**

In `SessionState::new()`, for an inbound connection the `remote_addr` is set to `RemoteAddress::Init` holding the peer's TCP source address (which includes the ephemeral port): [1](#0-0) 

**Step 2 — GetNodes handler with `listen_port=None` and `version=1`**

In `received()`, when `listen_port` is `None`, the `update_port()` call is skipped entirely: [2](#0-1) 

Then, unconditionally for `version >= REUSE_PORT_VERSION`, `change_to_listen()` is called: [3](#0-2) 

**Step 3 — `change_to_listen()` promotes the ephemeral address**

`change_to_listen()` blindly promotes `RemoteAddress::Init(addr)` → `RemoteAddress::Listen(addr)` without any port validation: [4](#0-3) 

Since `update_port()` was skipped, `addr` still contains the ephemeral TCP source port (e.g., 49152–60999).

**Step 4 — The ephemeral address enters the announce pipeline**

In `notify()`, `check_timer()` returns `Some(addr)` only when `remote_addr` is `RemoteAddress::Listen`: [5](#0-4) 

The only guard before adding to `announce_list` is `is_valid_addr`, which checks IP reachability (public vs. private), not port validity: [6](#0-5) [7](#0-6) 

If the attacker connects from a public IP, `is_reachable(ip)` returns `true`, and the ephemeral-port address passes the filter and enters `announce_list`.

**Step 5 — Broadcast to other peers**

The address is pushed into `announce_multiaddrs` of up to 3 random peers and sent as `Nodes { announce: true }`: [8](#0-7) 

Receiving peers call `add_new_addrs`, which stores the address in their peer store via `peer_store.add_addr`: [9](#0-8) 

**Conclusion**

The bug is real and the code path is concrete. An inbound peer with a public IP sends `GetNodes(version=1, listen_port=None)`. The handler skips `update_port()` but still calls `change_to_listen()`, promoting the ephemeral source port to a "listen" address. This address then propagates through the announce mechanism to other nodes' peer stores.

There is one partial mitigation: `node_flags(*id)` must return `Some`, meaning the peer must have completed the identify protocol handshake first. This is a normal part of connection setup, so it is not a meaningful barrier for a cooperative attacker.

**Impact:** Peer store pollution. Other nodes store and attempt connections to the ephemeral port, which is not listening. At scale (many such peers), this degrades peer discovery quality across the network. The `ANNOUNCE_INTERVAL` of 24 hours and the 3-peer fan-out limit the propagation rate but do not prevent it.

---

### Title
Inbound peer can inject ephemeral TCP port as listen address into peer store via `GetNodes(version=1, listen_port=None)` — (`network/src/protocols/discovery/mod.rs`)

### Summary
When an inbound peer sends `GetNodes` with `version >= 1` and `listen_port = None`, the handler skips `update_port()` but still calls `change_to_listen()`, promoting the ephemeral TCP source port to a `RemoteAddress::Listen`. This address is then broadcast to other peers via the announce mechanism and stored in their peer stores.

### Finding Description
In `received()` (`mod.rs` lines 124–139), the two operations are not atomic:
- `update_port(port)` is called only when `listen_port` is `Some`
- `change_to_listen()` is called whenever `version >= REUSE_PORT_VERSION`, regardless of whether `update_port` ran

`change_to_listen()` (`state.rs` lines 147–151) has no guard: it unconditionally converts `Init(addr)` → `Listen(addr)`. When `update_port` was skipped, `addr` still holds the ephemeral source port from the TCP handshake.

### Impact Explanation
The ephemeral-port address propagates via the announce pipeline to other nodes' peer stores. Those nodes waste connection attempts on unreachable addresses. A coordinated set of such peers can pollute the peer store of honest nodes, degrading peer discovery and network connectivity.

### Likelihood Explanation
Any inbound peer with a public IP can trigger this with a single crafted `GetNodes` message after completing the identify handshake. No special privileges or keys are required.

### Recommendation
Guard `change_to_listen()` so it is only called when `listen_port` was explicitly provided, or add a port-validity check inside `change_to_listen()` to reject ephemeral-range ports. The two branches should be mutually exclusive: either `update_port(port)` runs (which already transitions to `Listen`), or `change_to_listen()` is skipped.

### Proof of Concept
1. Connect inbound to a CKB node from a public IP with ephemeral source port P.
2. Complete the identify handshake so `node_flags` returns `Some`.
3. Send `GetNodes { version: 1, listen_port: None, count: 1000, required_flags: 0 }`.
4. Wait for the next `notify` tick (default 60s).
5. On a peer that received the announce, call `fetch_random_addrs` and observe an entry with port P — an ephemeral port that is not listening.

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

**File:** network/src/protocols/discovery/mod.rs (L124-135)
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
```

**File:** network/src/protocols/discovery/mod.rs (L136-139)
```rust
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

**File:** network/src/protocols/discovery/mod.rs (L347-362)
```rust
    fn add_new_addrs(&mut self, _session_id: SessionId, addrs: Vec<(Multiaddr, Flags)>) {
        if addrs.is_empty() {
            return;
        }

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

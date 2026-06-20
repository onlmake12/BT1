The code path is concrete and traceable. Let me map it precisely.

**Inbound connection initialization** (`state.rs` line 81): an inbound session starts with `RemoteAddress::Init(context.session.address.clone())` — the ephemeral source address of the TCP connection. [1](#0-0) 

**GetNodes handler** (`mod.rs` lines 136–139): after processing `listen_port` (which may be `None`), `change_to_listen()` is called unconditionally for any `version >= REUSE_PORT_VERSION` (≥ 1), regardless of whether a valid `listen_port` was supplied. [2](#0-1) 

**`change_to_listen()`** (`state.rs` lines 147–151): promotes `RemoteAddress::Init(addr)` → `RemoteAddress::Listen(addr)` with no verification — the ephemeral port is preserved as-is. [3](#0-2) 

**`check_timer()`** (`state.rs` lines 94–109): returns `Some(addr)` only when `remote_addr` is `RemoteAddress::Listen`. After the promotion above, the ephemeral address qualifies. [4](#0-3) 

**`notify()` broadcast** (`mod.rs` lines 231–261): every tick, `check_timer()` is called; if it returns an address that passes `is_valid_addr()` (a public-IP reachability check only — no port validation), it is pushed into `announce_list` and sent to up to 3 random peers. [5](#0-4) 

**`is_valid_addr()`** (`mod.rs` lines 332–341): only checks whether the IP is publicly reachable — it does not validate the port or confirm the peer is actually listening. [6](#0-5) 

The path is real and locally testable. The invariant is broken: `change_to_listen()` is gated only on the attacker's self-reported `version` field, not on any verified listen capability.

---

### Title
Inbound Peer Version Claim Unconditionally Promotes Ephemeral Address to Listen Status, Enabling Peer-Store Poisoning — (`network/src/protocols/discovery/mod.rs`, `state.rs`)

### Summary
An unprivileged inbound peer can send `GetNodes{version: 1, listen_port: None}` to cause its ephemeral TCP source address to be promoted to `RemoteAddress::Listen` and subsequently broadcast to up to 3 peers every 24 hours, poisoning their peer stores with unreachable addresses.

### Finding Description
When an inbound connection is established, `SessionState::new()` initializes `remote_addr` as `RemoteAddress::Init(ephemeral_addr)` — the source IP:port of the TCP connection. [1](#0-0) 

When the inbound peer sends `GetNodes` with `version >= 1`, the handler at lines 136–139 calls `state.remote_addr.change_to_listen()` unconditionally, regardless of whether `listen_port` was provided. [2](#0-1) 

`change_to_listen()` blindly upgrades `Init(addr)` to `Listen(addr)`, preserving the ephemeral port. [3](#0-2) 

Once promoted, `check_timer()` returns the address on the next 24-hour tick, and `notify()` adds it to `announce_list` for broadcast to 3 random peers — provided `is_valid_addr()` passes (public IP only, no port check). [7](#0-6) 

### Impact Explanation
Receiving peers call `add_new_addrs()` on the announced address, inserting `attacker_ip:ephemeral_port` into their peer stores. [8](#0-7) 
Those peers then attempt outbound connections to the bogus address (which fails), waste connection slots, and may re-announce the address further. A single attacker with a public IP maintaining one connection can inject one bogus address per 24 hours into the network's peer stores. Multiple simultaneous connections multiply the rate. Over time this degrades peer discovery quality network-wide.

### Likelihood Explanation
The attack requires only: (1) a public IP, (2) one inbound TCP connection to a victim node, (3) sending a single crafted `GetNodes` message. No special privileges, no PoW, no key material. The 24-hour delay limits throughput but does not prevent the attack.

### Recommendation
`change_to_listen()` should only be called when the peer has supplied a valid `listen_port` (i.e., inside the `if let Some(port) = listen_port` block, after `update_port()` has already set `RemoteAddress::Listen`). The `version >= REUSE_PORT_VERSION` check at lines 136–139 should be removed or made conditional on `listen_port` being present and validated. [9](#0-8) 

### Proof of Concept
```
1. Attacker (public IP A.B.C.D, ephemeral source port E) connects inbound to victim.
2. remote_addr = RemoteAddress::Init(A.B.C.D:E)
3. Attacker sends: GetNodes { version: 1, listen_port: None, count: 1, required_flags: 0 }
4. Handler: listen_port is None → skip update_port block.
5. version (1) >= REUSE_PORT_VERSION (1) → change_to_listen() called.
6. remote_addr = RemoteAddress::Listen(A.B.C.D:E)   ← ephemeral port promoted
7. After 24h, notify() tick: check_timer() returns A.B.C.D:E
8. is_valid_addr(A.B.C.D:E) → true (public IP)
9. announce_list = [(A.B.C.D:E, flags)]
10. Broadcast to 3 random peers → they store A.B.C.D:E in peer_store and attempt connections.
```

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

**File:** network/src/protocols/discovery/mod.rs (L231-261)
```rust
            if let Some(addr) = state
                .check_timer(now, ANNOUNCE_INTERVAL)
                .filter(|addr| self.addr_mgr.is_valid_addr(addr))
                && let Some(flags) = self.addr_mgr.node_flags(*id)
            {
                announce_list.push((addr.clone(), flags));
            }
        }

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

**File:** network/src/protocols/discovery/mod.rs (L347-363)
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
    }
```

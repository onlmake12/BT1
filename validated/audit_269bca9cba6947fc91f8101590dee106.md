The code is fully readable. Let me trace every step of the claimed path precisely.

**Step 1 — Inbound session initialization**

When an inbound peer connects, `SessionState::new()` branches on `is_outbound()`: [1](#0-0) 

Inbound sessions land on line 81: `RemoteAddress::Init(context.session.address.clone())` — the ephemeral source address of the TCP connection, **not** a listen address.

**Step 2 — GetNodes handler, the unconditional `change_to_listen()` call**

In `received()`, when a `GetNodes` message arrives: [2](#0-1) 

There is **no check** on whether the session is inbound or outbound. Any peer that sends `version >= REUSE_PORT_VERSION (1)` triggers `change_to_listen()`. With `listen_port=None`, the `update_port` branch (lines 124–135) is skipped entirely, so `remote_addr` is still `Init` when `change_to_listen()` is called.

**Step 3 — `change_to_listen()` promotes Init → Listen unconditionally** [3](#0-2) 

The ephemeral source address (e.g., `1.2.3.4:54321`) is now stored as `RemoteAddress::Listen(1.2.3.4:54321)`.

**Step 4 — `check_timer` gates on `RemoteAddress::Listen`** [4](#0-3) 

`last_announce` starts as `None`, so `.unwrap_or(true)` fires on the **first** `notify()` call (every 60 s). It returns `Some(addr)` only when `remote_addr` is `Listen` — which it now is.

**Step 5 — `notify()` broadcasts the address to up to 3 peers** [5](#0-4) 

Two guards remain:
- `is_valid_addr`: only rejects RFC-1918/loopback IPs — a globally routable attacker IP passes trivially.
- `node_flags`: requires `identify_info` to be set. The Identify protocol runs automatically on connection and completes in milliseconds, so by the time the first 60-second `notify()` fires, this is already populated.

The address is pushed into `announce_list` and fanned out to up to 3 random sessions.

---

### Title
Inbound peer can self-promote ephemeral address to `RemoteAddress::Listen` via `GetNodes(version≥1)`, causing it to be broadcast as a valid listen address through the discovery layer — (`network/src/protocols/discovery/mod.rs`)

### Summary
`received()` calls `state.remote_addr.change_to_listen()` for any peer that sends `GetNodes` with `version >= REUSE_PORT_VERSION`, without checking whether the session is inbound or outbound. For inbound sessions, `remote_addr` starts as `RemoteAddress::Init` (the ephemeral TCP source address). After promotion, `check_timer` returns this address, and `notify()` broadcasts it to up to 3 other sessions every `ANNOUNCE_INTERVAL`, with no port-reachability verification.

### Finding Description
The design intent of `change_to_listen()` is to handle Linux SO_REUSEPORT: on Linux, an outbound connection reuses the listen port, so the connection's source port IS the listen port. The version field signals this capability. However, the guard at mod.rs:136 applies to **all** sessions regardless of direction. An inbound peer's ephemeral source port is never its listen port, yet the code promotes it identically.

The full path:
1. Attacker connects inbound from a globally routable IP (e.g., `1.2.3.4:54321`).
2. `SessionState::new()` sets `remote_addr = RemoteAddress::Init(/ip4/1.2.3.4/tcp/54321)`.
3. Attacker sends `GetNodes { version: 0xFFFFFFFF, listen_port: None, ... }`.
4. `listen_port=None` skips `update_port`; `version >= 1` triggers `change_to_listen()`.
5. `remote_addr` becomes `RemoteAddress::Listen(/ip4/1.2.3.4/tcp/54321)`.
6. Within 60 seconds, `notify()` fires; `check_timer` returns the address (first call, `last_announce=None`); `is_valid_addr` passes (global IP); `node_flags` returns `Some` (identify completed).
7. `/ip4/1.2.3.4/tcp/54321` is pushed into `announce_list` and sent as `Nodes(announce=true)` to up to 3 peers.

### Impact Explanation
Every node that receives the announcement stores the invalid address in its peer store and may re-announce it further. With multiple coordinated inbound connections, an attacker can flood the discovery layer with attacker-controlled addresses (pointing to closed ports or honeypots), degrading peer discovery quality across the entire network. Legitimate nodes waste connection attempts on dead addresses, and the peer store's capacity can be consumed by garbage entries.

### Likelihood Explanation
The attack requires only an inbound TCP connection from a globally routable IP and a single crafted `GetNodes` message. No authentication, no PoW, no privileged role. The 60-second `notify()` interval means the address propagates within one minute of connection. The `DuplicateGetNodes` guard (line 110) prevents re-sending, but one message per connection is sufficient.

### Recommendation
Gate `change_to_listen()` on session direction. The call should only apply to outbound sessions (where SO_REUSEPORT makes the source port equal to the listen port):

```rust
// mod.rs, inside the GetNodes handler
if version >= state::REUSE_PORT_VERSION && session.ty.is_outbound() {
    state.remote_addr.change_to_listen();
}
```

For inbound peers, the listen address should only be accepted via the explicit `listen_port` field (which goes through `update_port`), not via the version-based promotion path.

### Proof of Concept
```
1. Connect to victim node as inbound (victim accepts the connection).
2. Complete the Identify handshake (automatic).
3. Send DiscoveryMessage::GetNodes { version: 0xFFFFFFFF, listen_port: None, count: 1, required_flags: 0 }.
4. Wait up to 60 seconds for notify() to fire.
5. Observe that other connected peers receive a Nodes(announce=true) message containing /ip4/<attacker_ip>/tcp/<ephemeral_port>.
6. Assert: no outgoing Nodes(announce=true) should contain an address whose port was never verified as a listen port.
``` [2](#0-1) [6](#0-5) [3](#0-2) [5](#0-4) [7](#0-6)

### Citations

**File:** network/src/protocols/discovery/state.rs (L45-82)
```rust
        let remote_addr = if context.session.ty.is_outbound() {
            let port = context
                .listens()
                .iter()
                .flat_map(|address| {
                    // Verify self is a public node first
                    // if not, try to make public network nodes broadcast hole punching information
                    if addr_manager.is_valid_addr(address) {
                        multiaddr_to_socketaddr(address).map(|socket_addr| socket_addr.port())
                    } else {
                        None
                    }
                })
                .next();

            let msg = encode(DiscoveryMessage::GetNodes {
                #[cfg(target_os = "linux")]
                version: REUSE_PORT_VERSION,
                #[cfg(not(target_os = "linux"))]
                version: FIRST_VERSION,
                count: MAX_ADDR_TO_SEND as u32,
                listen_port: port,
                required_flags: addr_manager.required_flags(),
            });

            if context.send_message(msg).await.is_err() {
                debug!(
                    "{:?} sending discovery msg GetNode failed",
                    context.session.id
                )
            }

            addr_known.insert(&context.session.address);

            RemoteAddress::Listen(context.session.address.clone())
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

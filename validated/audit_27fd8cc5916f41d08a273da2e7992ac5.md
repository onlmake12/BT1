Audit Report

## Title
Inbound peer promotes ephemeral TCP source address to `RemoteAddress::Listen` via `GetNodes(version≥1)`, enabling broadcast of unverified addresses through discovery layer — (`network/src/protocols/discovery/mod.rs`)

## Summary
`received()` calls `state.remote_addr.change_to_listen()` for any session that sends `GetNodes` with `version >= REUSE_PORT_VERSION (1)`, with no check on session direction. For inbound sessions, `remote_addr` is initialized as `RemoteAddress::Init(ephemeral_source_address)`. After promotion, `check_timer` returns this address on the first `notify()` call (within 60 seconds), and `notify()` fans it out to up to 3 peers as a `Nodes(announce=true)` message. Receiving peers add the address to their peer stores without any reachability verification.

## Finding Description
**Root cause**: `mod.rs:136–139` applies `change_to_listen()` unconditionally to all sessions:

```rust
if version >= state::REUSE_PORT_VERSION {
    // after enable reuse port, it can be broadcast
    state.remote_addr.change_to_listen();
}
```

The design intent (Linux SO_REUSEPORT) only holds for outbound sessions, where the kernel reuses the listen port as the TCP source port. For inbound sessions, the source port is an ephemeral OS-assigned port, never a listen port.

**Exploit path**:
1. Attacker connects inbound to victim. `SessionState::new()` sets `remote_addr = RemoteAddress::Init(/ip4/1.2.3.4/tcp/54321)` (state.rs:81).
2. Attacker sends `DiscoveryMessage::GetNodes { version: 0xFFFFFFFF, listen_port: None, ... }`.
3. `listen_port = None` skips `update_port` (mod.rs:124–135). `version >= 1` triggers `change_to_listen()` (mod.rs:136–139).
4. `remote_addr` becomes `RemoteAddress::Listen(/ip4/1.2.3.4/tcp/54321)` (state.rs:147–151).
5. First `notify()` fires within 60 seconds. `check_timer` returns the address because `last_announce = None` (state.rs:95–98, 101–102).
6. `is_valid_addr` passes for any globally routable IP (mod.rs:332–341). `node_flags` returns `Some` once Identify completes (mod.rs:234, 397–402).
7. `/ip4/1.2.3.4/tcp/54321` is pushed into `announce_list` and sent as `Nodes(announce=true)` to up to 3 random peers (mod.rs:243–258).
8. Receiving peers call `add_new_addrs`, which stores the address in their peer stores (mod.rs:205, 347–362).

**Existing guards are insufficient**:
- `DuplicateGetNodes` (mod.rs:110): prevents re-sending per connection, but one message per connection is enough.
- `is_valid_addr` (mod.rs:332–341): only rejects RFC-1918/loopback IPs; any globally routable attacker IP passes.
- `node_flags` (mod.rs:234): requires Identify to complete, which happens automatically well before the first 60-second timer fires.

## Impact Explanation
An attacker with multiple inbound connections (no authentication, no PoW required) can inject attacker-controlled addresses into the peer stores of victim nodes and their peers. Each injected address points to a port that is never a listen port. Nodes that attempt to connect to these addresses waste resources on failed connection attempts. With enough coordinated connections, the peer store capacity can be consumed by garbage entries, degrading peer discovery quality across the network. This maps to **High: Vulnerabilities or bad designs which could cause CKB network congestion with few costs** — the cost is only inbound TCP connections and a single crafted message per connection.

## Likelihood Explanation
The attack requires only: (1) a globally routable IP, (2) the ability to accept inbound TCP connections (so the victim can connect back, or the attacker connects to the victim), and (3) sending one crafted `GetNodes` message. No privilege, no authentication, no PoW. The 60-second `notify()` check interval means the first propagation occurs within one minute of connection. The attack is repeatable with as many connections as the victim's peer limit allows.

## Recommendation
Gate `change_to_listen()` on session direction. The SO_REUSEPORT promotion is only semantically valid for outbound sessions:

```rust
// mod.rs, inside the GetNodes handler
if version >= state::REUSE_PORT_VERSION && session.ty.is_outbound() {
    state.remote_addr.change_to_listen();
}
```

For inbound peers, the listen address should only be accepted via the explicit `listen_port` field (which goes through `update_port` at mod.rs:124–135), not via the version-based promotion path.

## Proof of Concept
1. Connect to a victim node as an inbound peer (victim accepts the TCP connection).
2. Complete the Identify handshake (automatic, handled by the Identify protocol).
3. Send `DiscoveryMessage::GetNodes { version: 0xFFFFFFFF, listen_port: None, count: 1, required_flags: 0 }`.
4. Wait up to 60 seconds for the first `notify()` call.
5. Observe that up to 3 of the victim's other connected peers receive a `Nodes(announce=true)` message containing `/ip4/<attacker_ip>/tcp/<ephemeral_port>`.
6. Verify those peers store the address via `add_new_addrs` → `peer_store.add_addr`.
7. Assert invariant: no `Nodes(announce=true)` message should contain an address whose port was never verified as a listen port. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4) [6](#0-5)

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

The code trace confirms this is a real vulnerability. Here is the complete analysis:

---

### Title
Unauthenticated `listen_port` in `GetNodes` Allows Peer Store Poisoning via Arbitrary IP:Port Injection — (`network/src/protocols/discovery/mod.rs`)

### Summary
An unprivileged inbound peer can supply an arbitrary `listen_port` value in a `GetNodes` discovery message. The node unconditionally replaces the TCP port of the peer's observed address with the attacker-supplied value and inserts the resulting forged address into the peer store. No port validation exists anywhere in the path. The forged address is then propagated to other nodes via periodic `Nodes` announcements.

### Finding Description

**Step 1 — Inbound session initialization.**
When an inbound peer connects, `SessionState::new` sets `remote_addr` to `RemoteAddress::Init(context.session.address.clone())` — the peer's real ephemeral address, e.g. `/ip4/1.2.3.4/tcp/54321`. [1](#0-0) 

**Step 2 — `GetNodes` handler calls `update_port` without validation.**
When the inbound peer sends `GetNodes { listen_port: Some(443), ... }`, the handler calls `state.remote_addr.update_port(port)` with the raw attacker-supplied value. [2](#0-1) 

**Step 3 — `update_port` unconditionally replaces the TCP port.**
`update_port` only guards on `RemoteAddress::Init`, which is exactly the state for inbound sessions. It replaces `Protocol::Tcp(_)` with `Protocol::Tcp(port)` and transitions to `RemoteAddress::Listen`. [3](#0-2) 

**Step 4 — Forged address is immediately inserted into the peer store.**
After `update_port`, `remote_addr` is `RemoteAddress::Listen(/ip4/1.2.3.4/tcp/443)`. The `if let RemoteAddress::Listen` branch matches and calls `add_new_addr` with the forged address. [4](#0-3) 

**Step 5 — `is_valid_addr` only checks IP reachability, not port validity.**
The sole filter before `peer_store.add_addr` is `is_reachable(socket_addr.ip())`. Any port value — including 0, 80, 443, or 65535 — passes unconditionally. [5](#0-4) 

**Step 6 — `peer_store.add_addr` performs no port validation.**
The peer store only checks the ban list before inserting the address. [6](#0-5) 

**Step 7 — Forged address is propagated to other peers.**
The `notify` loop calls `check_timer`, which returns `Some(addr)` only when `remote_addr` is `RemoteAddress::Listen`. After `update_port`, the forged address qualifies and is pushed into `announce_list`, then broadcast to up to 3 random peers every 24 hours. [7](#0-6) 

### Impact Explanation
An attacker connecting from a public IP can poison the peer store of every node they connect to with an arbitrary IP:port combination derived from their real IP. The forged entries are then gossiped to other nodes via `Nodes` announce messages, causing network-wide propagation. Honest nodes waste outbound connection slots attempting to reach wrong endpoints (e.g., web servers on port 80/443), degrading peer discovery and network connectivity. The `received_get_nodes` flag limits the attack to one poisoned entry per connection, but an attacker can open many parallel connections to scale the poisoning. [8](#0-7) 

### Likelihood Explanation
The attack requires only an inbound TCP connection from a public IP and a single crafted `GetNodes` message — no authentication, no PoW, no special role. It is trivially automatable and locally testable.

### Recommendation
Before calling `update_port`, validate that the supplied port is within an acceptable range (e.g., `1024..=65535` or at minimum `port != 0`). Additionally, consider rate-limiting or scoring peers that supply ports conflicting with well-known service ports. The `is_valid_addr` function should be extended to reject port 0 and optionally well-known reserved ports.

### Proof of Concept
1. Connect to a CKB node as an inbound peer from a public IP `1.2.3.4:54321`.
2. Send `DiscoveryMessage::GetNodes { version: 0, count: 1000, listen_port: Some(80), required_flags: Flags::COMPATIBILITY }`.
3. Assert that the victim node's peer store contains `/ip4/1.2.3.4/tcp/80` (not `/ip4/1.2.3.4/tcp/54321`).
4. Repeat with `listen_port: Some(0)` and `listen_port: Some(65535)` to confirm no port validation exists.
5. Wait for the victim's next `notify` tick and observe the forged address being broadcast in a `Nodes { announce: true }` message to other connected peers.

### Citations

**File:** network/src/protocols/discovery/state.rs (L80-82)
```rust
        } else {
            RemoteAddress::Init(context.session.address.clone())
        };
```

**File:** network/src/protocols/discovery/state.rs (L153-166)
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
```

**File:** network/src/protocols/discovery/mod.rs (L110-117)
```rust
                            if state.received_get_nodes && check(Misbehavior::DuplicateGetNodes) {
                                if context.disconnect(session.id).await.is_err() {
                                    debug!("Disconnect {:?} msg failed to send", session.id)
                                }
                                return;
                            }

                            state.received_get_nodes = true;
```

**File:** network/src/protocols/discovery/mod.rs (L124-125)
```rust
                            if let Some(port) = listen_port {
                                state.remote_addr.update_port(port);
```

**File:** network/src/protocols/discovery/mod.rs (L128-134)
```rust
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

**File:** network/src/peer_store/peer_store_impl.rs (L71-80)
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
    }
```

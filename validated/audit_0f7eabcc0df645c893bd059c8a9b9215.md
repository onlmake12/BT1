Now I have all the code needed to trace the full exploit path. Let me verify the `is_valid_addr` and `fetch_random_addrs` behavior to confirm propagation.

The full exploit path is confirmed. Here is the analysis:

---

### Title
Inbound Peer Can Inject Port-0 Address into Peer Store and Propagate It Network-Wide via Discovery Protocol — (`network/src/protocols/discovery/mod.rs`)

### Summary

An unprivileged inbound peer can send a `GetNodes` message with `listen_port=Some(0)` and `version=1`, causing the victim node to store a malformed `/ip4/X.X.X.X/tcp/0/p2p/...` address in its peer store and propagate it to up to 3 other peers via the periodic announce mechanism. No authentication or privilege is required.

### Finding Description

**Step 1 — Inbound session initialization.**
When an inbound peer connects, `SessionState::new()` sets `remote_addr = RemoteAddress::Init(session.address)`. [1](#0-0) 

**Step 2 — `GetNodes` with `listen_port=Some(0)` and `version=1`.**
In `received()`, the `if let Some(port) = listen_port` branch is entered with `port = 0` (Rust's `Option<u16>` treats `Some(0)` as a present value, not absent). [2](#0-1) 

**Step 3 — `update_port(0)` converts `Init` → `Listen` with port 0.**
`update_port` only operates on `RemoteAddress::Init`. It replaces `Protocol::Tcp(_)` with `Protocol::Tcp(0)` and then unconditionally sets `*self = RemoteAddress::Listen(addr)`. There is no guard against port 0. [3](#0-2) 

**Step 4 — `add_new_addr` is called immediately after with the port-0 address.**
The `if let RemoteAddress::Listen(ref addr) = state.remote_addr` check at line 128 now matches (because `update_port` just converted it), so `add_new_addr` is called with the port-0 multiaddr. [4](#0-3) 

**Step 5 — `is_valid_addr` does not validate the port.**
`add_new_addrs` filters addresses through `is_valid_addr`, which only calls `is_reachable(socket_addr.ip())`. It checks the IP for global reachability but performs **no port validation**. Port 0 with a public IP passes this filter. [5](#0-4) 

**Step 6 — `peer_store.add_addr` stores the port-0 address without validation.**
`add_addr` only checks the ban list and calls `check_purge`. No port-range validation exists. The address is stored as `AddrInfo` with `last_connected_at_ms = 0`. [6](#0-5) 

**Step 7 — `change_to_listen()` at line 138 is a no-op** (already `Listen` after `update_port`), so `version >= REUSE_PORT_VERSION` adds no additional protection. [7](#0-6) 

**Step 8 — Periodic `notify()` propagates the port-0 address to up to 3 peers.**
`check_timer` returns the address because `remote_addr` is now `RemoteAddress::Listen`. The `is_valid_addr` filter is applied again — same IP-only check, port 0 passes. The address is pushed into `announce_multiaddrs` and sent to up to 3 randomly selected peers. [8](#0-7) 

**Step 9 — `fetch_addrs_to_feeler` returns the port-0 address for connection attempts.**
Since the stored `AddrInfo` has `last_connected_at_ms = 0` and `attempts_count = 0`, it satisfies the feeler filter (never connected, not tried recently). The node will attempt TCP connections to port 0, which always fail. [9](#0-8) 

### Impact Explanation

- **Peer store pollution**: Every inbound peer can inject one port-0 address per session. With many inbound connections, the peer store fills with unreachable entries, degrading peer discovery quality and wasting feeler connection slots.
- **Network-wide propagation**: The `notify()` announce path propagates the port-0 address to up to 3 peers per 24-hour cycle. Those peers store and re-announce it, causing cascading pollution across the network's address tables.
- **Wasted connection attempts**: `fetch_addrs_to_feeler` returns port-0 entries, causing the node to repeatedly attempt TCP connections to port 0 (which the OS immediately rejects), consuming connection budget.

### Likelihood Explanation

The exploit requires only an inbound TCP connection and a single crafted `GetNodes` message. No key material, no PoW, no privileged role. Any node accepting inbound connections (the default) is vulnerable. The attack is trivially scriptable.

### Recommendation

Add a port validity check in `update_port` and/or `is_valid_addr`:

1. **In `update_port`** (`state.rs`): reject port 0 — do not convert to `Listen` if `port == 0`.
2. **In `is_valid_addr`** (`mod.rs`): after `multiaddr_to_socketaddr`, also verify `socket_addr.port() != 0`.
3. **In `add_addr`** (`peer_store_impl.rs`): validate that the stored multiaddr contains a non-zero TCP port.

### Proof of Concept

```
1. Connect to victim node as inbound peer (victim accepts inbound connections by default).
2. After the discovery protocol handshake, send:
     DiscoveryMessage::GetNodes {
         listen_port: Some(0),
         version: 1,          // >= REUSE_PORT_VERSION
         count: 1,
         required_flags: 0,
     }
3. Wait for the next notify() tick (up to ANNOUNCE_CHECK_INTERVAL = 60s).
4. Inspect the victim's peer store: the entry /ip4/<attacker_ip>/tcp/0/p2p/<attacker_peer_id>
   will be present.
5. Connect a second peer to the victim and observe incoming Nodes(announce=true) messages:
   the port-0 address will appear in the announce payload sent to up to 3 peers.
```

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

**File:** network/src/protocols/discovery/mod.rs (L136-139)
```rust
                            if version >= state::REUSE_PORT_VERSION {
                                // after enable reuse port, it can be broadcast
                                state.remote_addr.change_to_listen();
                            }
```

**File:** network/src/protocols/discovery/mod.rs (L231-258)
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

**File:** network/src/peer_store/peer_store_impl.rs (L217-239)
```rust
    pub fn fetch_addrs_to_feeler<F>(&mut self, count: usize, filter: F) -> Vec<AddrInfo>
    where
        F: Fn(&AddrInfo) -> bool,
    {
        // Get info:
        // 1. Not already connected
        // 2. Not already tried in a minute
        // 3. Not connected within 3 days

        let now_ms = ckb_systemtime::unix_time_as_millis();
        let addr_expired_ms = now_ms.saturating_sub(ADDR_TRY_TIMEOUT_MS);
        let peers = &self.connected_peers;

        let filter = |peer_addr: &AddrInfo| {
            filter(peer_addr)
                && extract_peer_id(&peer_addr.addr)
                    .map(|peer_id| !peers.contains_key(&peer_id))
                    .unwrap_or_default()
                && !peer_addr.tried_in_last_minute(now_ms)
                && !peer_addr.connected(|t| t > addr_expired_ms)
        };

        self.addr_manager.fetch_random(count, filter)
```

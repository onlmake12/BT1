Audit Report

## Title
Port-Zero Address Injection via Discovery `GetNodes` — (`network/src/protocols/discovery/state.rs`, `network/src/protocols/discovery/mod.rs`)

## Summary

`update_port` in `state.rs` unconditionally replaces the TCP port with the caller-supplied value, including `0`, and transitions the state to `RemoteAddress::Listen`. An inbound peer sending `GetNodes{listen_port: Some(0)}` causes the receiving node to store `/ip4/<peer_ip>/tcp/0/p2p/<peer_id>` in its peer store and broadcast it to up to three connected peers via the periodic `notify` path. The address is also eligible for feeler dial attempts, each of which fails and increments `attempts_count` until the entry is marked non-connectable after three tries.

## Finding Description

**Root cause — `update_port` accepts port 0.**

`update_port` maps every `Protocol::Tcp(_)` to `Protocol::Tcp(port)` with no `port != 0` guard and unconditionally transitions state to `RemoteAddress::Listen`. [1](#0-0) 

**Call site — no pre-filter.**

`mod.rs` line 124 calls `update_port` for any `Some(port)`, including `Some(0)`. [2](#0-1) 

**Immediate peer-store insertion.**

Because `update_port` always produces `RemoteAddress::Listen`, the `if let RemoteAddress::Listen` branch always matches and calls `add_new_addr`. [3](#0-2) 

**`is_valid_addr` does not check port.**

For a multiaddr containing a `/p2p/` component, `multiaddr_to_socketaddr` returns `None`, so `is_valid_addr` returns `true` unconditionally via the `None => true` arm. [4](#0-3) 

**`add_addr` / `AddrInfo::new` do not check port.**

`add_addr` only checks the ban list. `AddrInfo::new` calls `base_addr`, which strips only `Ws/Wss/Memory/Tls` — `Tcp(0)` is preserved. [5](#0-4) [6](#0-5) 

**Broadcast path — `notify`/`check_timer`.**

`SessionState.last_announce` is `None` on construction, so `check_timer`'s `unwrap_or(true)` fires on the very first `notify` tick (60 seconds after connection). `is_valid_addr` returns `true` for the port-0 multiaddr. If the attacker's session has identify info (`node_flags` returns `Some`), the address is pushed into `announce_list` and forwarded to up to three connected peers. [7](#0-6) [8](#0-7) 

**`fetch_random_addrs` does NOT return the port-0 address** (correctly noted in the report): it requires `peer_addr.connected(|t| t > addr_expired_ms)`, which is `false` for `last_connected_at_ms = 0`. [9](#0-8) 

**Feeler-connection waste.**

`fetch_addrs_to_feeler` requires `!peer_addr.connected(|t| t > addr_expired_ms)`, which is `true` for `last_connected_at_ms = 0`, so the port-0 address is eligible. After `ADDR_MAX_RETRIES = 3` failed attempts, `is_connectable` marks it non-connectable. [10](#0-9) [11](#0-10) 

## Impact Explanation

Concrete impact: (1) one invalid peer-store slot consumed per attacker connection on the receiving node; (2) the port-0 address is broadcast to up to three of that node's peers on the first `notify` tick; (3) each receiving peer wastes up to three feeler dial attempts against port 0 before discarding the entry. Propagation is one hop — downstream peers do not re-broadcast via `notify` because `check_timer` only surfaces the address of the directly connected session, not peer-store entries. No path to node crash, consensus deviation, or economy damage exists. This maps to **Low (501–2000 points) — any other important performance improvement for CKB**: wasted dial resources and peer-store slot consumption.

## Likelihood Explanation

Requires only an inbound TCP connection — no authentication, no PoW, no privileged role. The attacker must hold the connection for one `ANNOUNCE_CHECK_INTERVAL` (60 seconds) and complete the identify handshake for the broadcast to fire. The `received_get_nodes` flag prevents duplicate injections per session, so each connection yields exactly one poisoned address. Trivially repeatable with many connections. [12](#0-11) 

## Recommendation

Add a `port != 0` guard in `update_port` before accepting the caller-supplied value:

```rust
// network/src/protocols/discovery/state.rs
pub(crate) fn update_port(&mut self, port: u16) {
    if port == 0 {
        return;
    }
    // ... existing logic
}
```

Alternatively, filter at the call site in `mod.rs` line 124:

```rust
if let Some(port) = listen_port.filter(|&p| p != 0) {
    state.remote_addr.update_port(port);
```

## Proof of Concept

```rust
// Minimal unit test sketch
let mut remote = RemoteAddress::Init("/ip4/1.2.3.4/tcp/54321/p2p/QmFoo".parse().unwrap());
remote.update_port(0);
// Assert: remote == RemoteAddress::Listen("/ip4/1.2.3.4/tcp/0/p2p/QmFoo")

// Peer-store insertion
let mut peer_store = PeerStore::default();
peer_store.add_addr("/ip4/1.2.3.4/tcp/0/p2p/QmFoo".parse().unwrap(), Flags::COMPATIBILITY).unwrap();
// Assert: peer_store.addr_manager().count() == 1

// Feeler eligibility (last_connected_at_ms == 0, attempts_count == 0)
let addrs = peer_store.fetch_addrs_to_feeler(10, |_| true);
// Assert: addrs contains the port-0 entry

// Broadcast eligibility via notify/check_timer:
// Connect attacker session, complete identify handshake, call notify after
// ANNOUNCE_CHECK_INTERVAL (60s), assert announce_multiaddrs of a second
// session contains the port-0 multiaddr.
```

### Citations

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

**File:** network/src/peer_store/peer_store_impl.rs (L230-239)
```rust
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

**File:** network/src/peer_store/peer_store_impl.rs (L276-279)
```rust
        let filter = |peer_addr: &AddrInfo| {
            required_flags_filter(required_flags, Flags::from_bits_truncate(peer_addr.flags))
                && peer_addr.connected(|t| t > addr_expired_ms)
        };
```

**File:** network/src/peer_store/mod.rs (L92-104)
```rust
pub(crate) fn base_addr(addr: &Multiaddr) -> Multiaddr {
    addr.iter()
        .filter_map(|p| {
            if matches!(
                p,
                Protocol::Ws | Protocol::Wss | Protocol::Memory(_) | Protocol::Tls(_)
            ) {
                None
            } else {
                Some(p)
            }
        })
        .collect()
```

**File:** network/src/peer_store/types.rs (L89-97)
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
```

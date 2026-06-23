### Title
Port-Zero Address Injection via Discovery GetNodes — (`network/src/protocols/discovery/state.rs`, `network/src/protocols/discovery/mod.rs`)

### Summary

An unprivileged inbound peer can send `GetNodes{listen_port: Some(0)}` to cause the receiving node to store `/ip4/<peer_ip>/tcp/0/p2p/<peer_id>` in its peer store and subsequently broadcast it to other nodes. No guard rejects port 0 at any point in the pipeline.

### Finding Description

**Step 1 — Attacker sends GetNodes with port 0.**

An inbound session triggers the `received` handler. The `listen_port` field is `Option<u16>`, so `Some(0)` is a valid encoding. [1](#0-0) 

**Step 2 — `update_port(0)` blindly replaces the TCP component.**

`update_port` maps every `Protocol::Tcp(_)` to `Protocol::Tcp(port)` with no `port != 0` guard, then transitions the state to `RemoteAddress::Listen`. [2](#0-1) 

**Step 3 — The Listen variant is immediately added to the peer store.**

Because `update_port` always produces `RemoteAddress::Listen`, the `if let RemoteAddress::Listen` branch always matches and calls `add_new_addr`. [3](#0-2) 

**Step 4 — `add_new_addrs` / `is_valid_addr` does not check port.**

`is_valid_addr` only inspects the IP for reachability via `is_reachable`. Port 0 is never examined. [4](#0-3) 

For a multiaddr containing a `/p2p/` component, `multiaddr_to_socketaddr` returns `None`, so `is_valid_addr` returns `true` unconditionally (the `None => true` arm). [5](#0-4) 

**Step 5 — `add_addr` / `AddrInfo::new` does not check port.**

`add_addr` only checks the ban list. `AddrInfo::new` calls `base_addr`, which strips only `Ws/Wss/Memory/Tls` — `Tcp(0)` is preserved. [6](#0-5) [7](#0-6) 

**Step 6 — The poisoned address is eligible for broadcast.**

`fetch_random` in `AddrManager` falls into the `None` branch for addresses with a `/p2p/` component. A freshly inserted address has `last_connected_at_ms = 0` and `attempts_count = 0`, so `is_connectable` returns `true` (`attempts_count < ADDR_MAX_RETRIES = 3`). The address is included in random selections and sent to other peers in `Nodes` announce messages. [8](#0-7) [9](#0-8) 

### Impact Explanation

- `/ip4/X/tcp/0/p2p/...` is stored in the peer store (up to `ADDR_COUNT_LIMIT = 16384` slots).
- The address is broadcast to connected peers via announce `Nodes` messages, propagating the invalid entry across the network.
- Every node that receives it will attempt to connect to port 0, fail, increment `attempts_count`, and eventually mark it non-connectable — but only after `ADDR_MAX_RETRIES = 3` or `ADDR_MAX_FAILURES = 10` failed attempts, wasting dial resources.
- If the peer store is near capacity, the eviction logic (`check_purge`) may displace a legitimate address to make room for the port-0 entry. [10](#0-9) 

### Likelihood Explanation

The attack requires only an inbound TCP connection — no authentication, no PoW, no privileged role. `GetNodes` is sent once per session (the `received_get_nodes` flag prevents duplicates), so each attacker connection injects exactly one poisoned address. The impact per connection is low, but the path is fully concrete and locally testable. [11](#0-10) 

### Recommendation

Add a `port != 0` guard in `update_port` before accepting the caller-supplied value:

```rust
// state.rs
pub(crate) fn update_port(&mut self, port: u16) {
    if port == 0 {
        return; // reject invalid listen port
    }
    // ... existing logic
}
```

Alternatively, add the check at the call site in `mod.rs` line 124:

```rust
if let Some(port) = listen_port.filter(|&p| p != 0) {
```

### Proof of Concept

```rust
// Unit test sketch
let mut state = SessionState { remote_addr: RemoteAddress::Init("/ip4/1.2.3.4/tcp/54321/p2p/Qm...".parse().unwrap()), ... };
state.remote_addr.update_port(0);
// Assert: remote_addr is now Listen(/ip4/1.2.3.4/tcp/0/p2p/Qm...)
// Assert: after add_new_addr, peer_store contains /ip4/1.2.3.4/tcp/0/p2p/Qm...
// Assert: fetch_random_addrs returns the port-0 address (is_connectable = true, attempts=0)
``` [12](#0-11)

### Citations

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

**File:** network/src/peer_store/mod.rs (L26-35)
```rust
pub(crate) const ADDR_COUNT_LIMIT: usize = 16384;
/// Consider we never seen a peer if peer's last_connected_at beyond this timeout
const ADDR_TIMEOUT_MS: u64 = 7 * 24 * 3600 * 1000;
/// The timeout that peer's address should be added to the feeler list again
pub(crate) const ADDR_TRY_TIMEOUT_MS: u64 = 3 * 24 * 3600 * 1000;
/// When obtaining the list of selectable nodes for identify,
/// the node that has just been disconnected needs to be excluded
pub(crate) const DIAL_INTERVAL: u64 = 15 * 1000;
const ADDR_MAX_RETRIES: u32 = 3;
const ADDR_MAX_FAILURES: u32 = 10;
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

**File:** network/src/peer_store/addr_manager.rs (L74-82)
```rust
                None => {
                    if filter(&addr_info) {
                        if addr_info.is_connectable(now_ms)
                            || addr_info
                                .addr
                                .iter()
                                .any(|p| matches!(p, Protocol::Onion3(_)))
                        {
                            addr_infos.push(addr_info);
```

**File:** network/src/peer_store/types.rs (L89-104)
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
```

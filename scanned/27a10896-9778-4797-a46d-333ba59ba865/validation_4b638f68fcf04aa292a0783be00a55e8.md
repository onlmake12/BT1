Audit Report

## Title
Inbound Peer Ephemeral Source Port Unconditionally Promoted to Listen Address via `change_to_listen()` When `listen_port=None` — (`network/src/protocols/discovery/mod.rs`, `network/src/protocols/discovery/state.rs`)

## Summary
When an inbound peer sends `GetNodes{version: 1, listen_port: None}`, the victim node unconditionally promotes the peer's ephemeral TCP source port to a `RemoteAddress::Listen` entry via `change_to_listen()`. On the first 60-second notify tick, this ephemeral-port address passes all existing guards and is broadcast as a valid listen address to up to 3 peers, who store it in their peer databases. The stored address is unreachable, polluting peer stores network-wide.

## Finding Description

**Step 1 — Inbound session initialization**

For inbound sessions, `SessionState::new` stores the peer's ephemeral source address as `RemoteAddress::Init`:

```rust
} else {
    RemoteAddress::Init(context.session.address.clone())
};
``` [1](#0-0) 

**Step 2 — `GetNodes` handler with `listen_port=None`**

When `listen_port` is `None`, the `if let Some(port)` block is skipped entirely. Execution then falls through unconditionally to `change_to_listen()`:

```rust
if let Some(port) = listen_port {
    state.remote_addr.update_port(port);
    // ...
}
if version >= state::REUSE_PORT_VERSION {
    state.remote_addr.change_to_listen();
}
``` [2](#0-1) 

**Step 3 — `change_to_listen()` promotes the ephemeral address verbatim**

Because `listen_port` was `None`, `remote_addr` is still `Init`. `change_to_listen()` clones the ephemeral address into `Listen` with no port rewriting or validation:

```rust
pub(crate) fn change_to_listen(&mut self) {
    if let RemoteAddress::Init(addr) = self {
        *self = RemoteAddress::Listen(addr.clone());
    }
}
``` [3](#0-2) 

**Step 4 — `check_timer()` fires on the first 60-second tick**

`last_announce` is `None` at session creation, so `unwrap_or(true)` fires on the very first `notify()` call (60 seconds after connection), returning the now-`Listen` ephemeral address: [4](#0-3) 

**Step 5 — Both guards in `notify()` are insufficient**

```rust
if let Some(addr) = state
    .check_timer(now, ANNOUNCE_INTERVAL)
    .filter(|addr| self.addr_mgr.is_valid_addr(addr))
    && let Some(flags) = self.addr_mgr.node_flags(*id)
``` [5](#0-4) 

- `is_valid_addr` only calls `is_reachable(socket_addr.ip())` — it checks the IP for public reachability but does not validate the port. A public attacker IP with an ephemeral port passes. [6](#0-5) 

- `node_flags` requires `identify_info` to be populated, which occurs during the normal P2P identify handshake — a standard step any connecting peer completes. [7](#0-6) 

**Step 6 — Broadcast to 3 peers and peer store insertion**

The ephemeral address is pushed into `announce_list` and queued into `announce_multiaddrs` for up to 3 randomly selected sessions. `send_messages` delivers it as `Nodes{announce: true}`. Receiving peers call `add_new_addrs` → `peer_store.add_addr`, permanently storing the unreachable address. [8](#0-7) 

## Impact Explanation

The bug causes the CKB peer store — the network's state storage for peer discovery — to accumulate unreachable addresses. Each attacker connection injects one garbage entry into up to 3 peer stores per 24-hour announce cycle (with the first injection occurring after just 60 seconds). With multiple simultaneous attacker connections, pollution scales linearly. Nodes attempting outbound connections to these addresses waste connection slots and fail. Over time, peer stores degrade in quality, impairing peer discovery across the network.

This matches: **Medium (2001–10000 points) — Suboptimal implementation of CKB state storage mechanism**, specifically the peer store, which is the persistent state used for peer discovery.

## Likelihood Explanation

Preconditions are minimal and fully within reach of any external unprivileged actor:
1. Establish a TCP connection to any node accepting inbound connections (standard P2P).
2. Complete the identify handshake (normal protocol flow, required for `node_flags` to return `Some`).
3. Send one `GetNodes{version: 1, listen_port: None}` message.
4. Hold the connection for 60 seconds.

No special privileges, no proof-of-work, no key material, and no victim mistakes are required. The attack is repeatable and scalable.

## Recommendation

Gate `change_to_listen()` on `listen_port` being `Some`, or restrict it to outbound sessions only. For inbound sessions, the remote's source port is always ephemeral and is never a stable listen address. The SO_REUSEPORT semantic (where source port equals listen port) only applies when the peer explicitly advertises a `listen_port`:

```rust
if let Some(port) = listen_port {
    state.remote_addr.update_port(port);
    // ... existing add_new_addr logic ...
} else if version >= state::REUSE_PORT_VERSION
    && context.session.ty.is_outbound()
{
    // Only valid for outbound sessions using SO_REUSEPORT
    state.remote_addr.change_to_listen();
}
```

This ensures `change_to_listen()` is never called for inbound sessions when no explicit listen port was declared.

## Proof of Concept

```
1. Attacker at /ip4/1.2.3.4/tcp/54321 connects inbound to victim node.
   → victim: remote_addr = Init(/ip4/1.2.3.4/tcp/54321)

2. Attacker completes identify handshake.
   → node_flags(session_id) = Some(flags)

3. Attacker sends: GetNodes { version: 1, listen_port: None, count: 1, required_flags: 0 }
   → listen_port is None → update_port() skipped
   → version(1) >= REUSE_PORT_VERSION(1) → change_to_listen() called
   → remote_addr = Listen(/ip4/1.2.3.4/tcp/54321)  ← ephemeral port, no rewrite

4. After 60 seconds, notify() fires:
   → check_timer(): last_announce=None → unwrap_or(true) → returns Listen addr
   → is_valid_addr(1.2.3.4): is_reachable(1.2.3.4) = true → passes
   → node_flags(session_id): Some(...) → passes
   → announce_list = [(/ip4/1.2.3.4/tcp/54321, flags)]

5. announce_list broadcast to ≤3 peers as Nodes{announce:true}.
   → receiving peers: add_new_addrs → peer_store.add_addr(/ip4/1.2.3.4/tcp/54321)
   → future connection attempts to port 54321 fail (ephemeral, closed)

Repeat with N attacker connections → N unreachable entries injected per cycle.
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

**File:** network/src/protocols/discovery/mod.rs (L397-402)
```rust
    fn node_flags(&self, id: SessionId) -> Option<Flags> {
        self.network_state.with_peer_registry(|reg| {
            reg.get_peer(id)
                .and_then(|peer| peer.identify_info.as_ref().map(|a| a.flags))
        })
    }
```

Based on my analysis of the production code, this is a real vulnerability.

---

### Title
Inbound Peer Can Promote Ephemeral Address to Listen via `change_to_listen` Without a Valid `listen_port` — (`network/src/protocols/discovery/mod.rs`)

### Summary

An unprivileged inbound peer can send a `GetNodes` message with `version=1` (`REUSE_PORT_VERSION`) and `listen_port=None`, causing the discovery protocol to unconditionally promote the peer's ephemeral TCP source address to `RemoteAddress::Listen`. On the next notify tick, this ephemeral address is gossiped to up to 3 other peers as a confirmed listen address, poisoning their peer discovery tables.

### Finding Description

**Inbound session initialization** (`state.rs` line 81): for inbound connections, `remote_addr` is set to `RemoteAddress::Init(context.session.address.clone())`, where `context.session.address` is the peer's source IP and **ephemeral** TCP port. [1](#0-0) 

**GetNodes handler** (`mod.rs` lines 124–139): the `listen_port` branch and the `version` branch are independent `if` blocks. When `listen_port=None`, `update_port` is skipped, but `change_to_listen()` is still called unconditionally whenever `version >= REUSE_PORT_VERSION`: [2](#0-1) 

**`change_to_listen()`** (`state.rs` lines 147–151): promotes `Init(ephemeral_addr)` → `Listen(ephemeral_addr)` with no validation of whether the address is actually a listen port: [3](#0-2) 

**notify tick** (`mod.rs` lines 231–237): `check_timer` returns `Some(addr)` on the first tick (since `last_announce` starts as `None`). The only guards are `is_valid_addr` (checks IP reachability, not port validity) and `node_flags` (requires `identify_info` from the identify protocol — satisfied by any peer completing the normal P2P handshake): [4](#0-3) 

**Broadcast**: the ephemeral address is pushed into `announce_multiaddrs` of up to 3 peer sessions and sent as a `Nodes{announce: true}` message, causing recipients to call `add_new_addrs` and persist the bogus address in their peer store: [5](#0-4) 

The design intent of `REUSE_PORT_VERSION` is that when `SO_REUSEPORT` is active, the connection source port equals the listen port, so promotion is safe. But the code does not verify that `listen_port` was actually provided before calling `change_to_listen()`, breaking the invariant for peers that send `version=1` with `listen_port=None`.

### Impact Explanation

Receiving peers add the attacker's ephemeral address to their peer store via `add_new_addrs`. They will subsequently attempt outbound connections to a port that is not listening, wasting connection slots and polluting the peer store. With multiple simultaneous inbound connections, an attacker can amplify the poisoning. The propagation per tick is bounded to 3 peers, so it is not instantaneously "network-wide," but it is persistent and repeatable.

### Likelihood Explanation

The exploit requires only: (1) establishing an inbound TCP connection, (2) completing the standard identify protocol handshake (normal P2P behavior), and (3) sending a single crafted `GetNodes` message. No special privileges, keys, or hashpower are needed. The `ANNOUNCE_CHECK_INTERVAL` is 60 seconds, so the first gossip fires within one minute of connection.

### Recommendation

Before calling `change_to_listen()`, verify that `listen_port` was explicitly provided. If `listen_port=None`, the peer has not declared a listen address and the ephemeral address must not be promoted regardless of the version field:

```rust
if let Some(port) = listen_port {
    state.remote_addr.update_port(port);
    // add to addr_mgr ...
} else if version >= state::REUSE_PORT_VERSION {
    // Only promote if the peer actually declared a listen port
    // (listen_port=None means no listen address; do not promote)
}
// Remove the unconditional change_to_listen() call below
```

Alternatively, gate `change_to_listen()` on `listen_port.is_some()`.

### Proof of Concept

```
1. Attacker opens inbound TCP connection to victim node from public IP 1.2.3.4:54321
2. Identify protocol completes → identify_info set, node_flags returns Some
3. Attacker sends: GetNodes { version: 1, listen_port: None, count: 1000, required_flags: 0 }
4. Handler: listen_port=None → update_port skipped
           version=1 >= REUSE_PORT_VERSION → change_to_listen() called
           remote_addr transitions: Init(1.2.3.4:54321) → Listen(1.2.3.4:54321)
5. Within 60s, notify fires:
   check_timer returns Some(1.2.3.4:54321) [last_announce was None]
   is_valid_addr(1.2.3.4:54321) → true (public IP)
   node_flags → Some(flags)
   → announce_list = [(1.2.3.4:54321, flags)]
6. Up to 3 other sessions receive Nodes{announce:true, items:[1.2.3.4:54321]}
7. Recipients call add_new_addrs → 1.2.3.4:54321 persisted in peer store
8. Recipients attempt connection to ephemeral port → fail → slot wasted
```

### Citations

**File:** network/src/protocols/discovery/state.rs (L80-82)
```rust
        } else {
            RemoteAddress::Init(context.session.address.clone())
        };
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

**File:** network/src/protocols/discovery/mod.rs (L243-258)
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
```

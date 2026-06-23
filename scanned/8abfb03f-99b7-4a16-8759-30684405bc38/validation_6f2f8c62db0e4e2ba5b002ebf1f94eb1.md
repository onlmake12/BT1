The code path is fully traceable. Let me map it precisely.

**Step 1 — Inbound session initialization**

When an inbound peer connects, `SessionState::new` sets `remote_addr` to `RemoteAddress::Init(context.session.address.clone())` — the ephemeral source port address. [1](#0-0) 

**Step 2 — Attacker sends `GetNodes` with `version=1`, `listen_port=None`**

In `received()`, the `listen_port` branch is skipped entirely when `listen_port` is `None`: [2](#0-1) 

**Step 3 — `change_to_listen()` is unconditionally called**

The version check at line 136 passes (`version >= REUSE_PORT_VERSION`), and `change_to_listen()` is called with no port correction: [3](#0-2) 

`change_to_listen()` blindly promotes `Init(ephemeral_addr)` → `Listen(ephemeral_addr)`: [4](#0-3) 

**Step 4 — The ephemeral address enters the announce pipeline**

In `notify()`, `check_timer()` returns `Some(addr)` only for `RemoteAddress::Listen` variants. After promotion, the ephemeral-port address passes this gate, passes `is_valid_addr()` (public IP check), and is pushed to `announce_list`: [5](#0-4) 

It is then queued for broadcast to up to 3 randomly selected peers: [6](#0-5) 

Those peers receive it as an `announce=true` `Nodes` message and call `add_new_addrs()` → `peer_store.add_addr()`.

**Step 5 — Design intent vs. actual behavior**

The `REUSE_PORT_VERSION` feature was designed for Linux SO_REUSEPORT, where the outbound source port genuinely equals the listen port. The outbound side only sends `version=REUSE_PORT_VERSION` on Linux: [7](#0-6) 

But the inbound receiver applies no such platform check — it trusts the peer-supplied `version` field unconditionally. An attacker on any OS can send `version=1` with `listen_port=None`, bypassing the `update_port()` correction path entirely.

---

### Title
Inbound peer can promote ephemeral source port to `RemoteAddress::Listen` via `GetNodes` version≥1 with `listen_port=None`, causing peer store pollution — (`network/src/protocols/discovery/mod.rs`)

### Summary
An unprivileged inbound peer sends a `GetNodes` message with `version=1` and `listen_port=None`. The `listen_port` branch (which calls `update_port()` to correct the port) is skipped, but the `version >= REUSE_PORT_VERSION` branch still calls `change_to_listen()`, promoting the ephemeral source-port address to `RemoteAddress::Listen`. This address is then broadcast to up to 3 peers per announce cycle, polluting their peer stores with an unconnectable address.

### Finding Description
In `DiscoveryProtocol::received` (`mod.rs` lines 124–139), the two branches are independent:

1. `if let Some(port) = listen_port` — calls `update_port(port)` to replace the ephemeral port with the declared listen port, then adds the corrected address to the addr manager.
2. `if version >= REUSE_PORT_VERSION` — calls `change_to_listen()` to promote `Init` → `Listen`.

When `listen_port=None`, branch 1 is skipped but branch 2 still executes. `change_to_listen()` has no guard for the case where no port correction has occurred:

```rust
pub(crate) fn change_to_listen(&mut self) {
    if let RemoteAddress::Init(addr) = self {
        *self = RemoteAddress::Listen(addr.clone()); // ephemeral port preserved
    }
}
```

The promoted address (with ephemeral port, e.g., `:54321`) then flows through `notify()` → `check_timer()` → `announce_list` → broadcast to 3 peers → `add_new_addrs()` → peer store.

### Impact Explanation
Receiving peers add the invalid address (correct public IP, wrong ephemeral port) to their peer stores. Connection attempts to that address fail. With multiple attacker sessions, the peer stores of connected nodes accumulate stale/invalid entries. The effect can cascade as those peers re-announce the address to their own peers.

### Likelihood Explanation
Any inbound TCP connection can trigger this. No authentication, no PoW, no special privilege required. The attacker only needs to complete the P2P handshake and send a single crafted `GetNodes` message. The `node_flags()` check (requiring `identify_info`) is satisfied by completing the normal identify protocol handshake, which is part of standard connection setup.

### Recommendation
Add a guard in `received()` so that `change_to_listen()` is only called when `listen_port` was provided (i.e., `update_port()` already corrected the address), or alternatively, only call `change_to_listen()` when `listen_port` is `Some`:

```rust
if let Some(port) = listen_port {
    state.remote_addr.update_port(port);
    // ... add to addr_mgr ...
    if version >= state::REUSE_PORT_VERSION {
        // already promoted by update_port(); no-op for Listen variant
    }
} else if version >= state::REUSE_PORT_VERSION {
    // Only promote if source port == listen port (Linux reuse-port assumption)
    // but we have no way to verify this without listen_port being declared;
    // do NOT call change_to_listen() here.
}
```

The safest fix: remove the standalone `change_to_listen()` call and only promote to `Listen` inside the `listen_port = Some(port)` branch (after `update_port()`), since `update_port()` already transitions to `RemoteAddress::Listen`.

### Proof of Concept
1. Establish an inbound session to a victim node (attacker has a public IP).
2. Complete the identify protocol handshake (so `node_flags()` returns `Some`).
3. Send `GetNodes { version: 1, listen_port: None, count: 10, required_flags: 0 }`.
4. Wait up to 60 seconds for the next `notify()` tick.
5. Observe that up to 3 peers connected to the victim receive an `announce=true` `Nodes` message containing the attacker's IP with the ephemeral source port.
6. Assert those peers' peer stores contain the invalid address.

Unit test: construct a `SessionState` with an inbound session (ephemeral port e.g. `:54321`), call the `GetNodes` handler with `version=1` and `listen_port=None`, then assert `state.remote_addr` is still `RemoteAddress::Init(...)` — the test will **fail** on the current code, confirming the bug.

### Citations

**File:** network/src/protocols/discovery/state.rs (L61-64)
```rust
                #[cfg(target_os = "linux")]
                version: REUSE_PORT_VERSION,
                #[cfg(not(target_os = "linux"))]
                version: FIRST_VERSION,
```

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

**File:** network/src/protocols/discovery/mod.rs (L245-258)
```rust
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

The exploit path is concrete and traceable through the code. Here is the full analysis:

---

### Title
Unverified `listen_port` in `GetNodes` Allows Inbound Peer to Inject Arbitrary Port into Peer Store — (`network/src/protocols/discovery/mod.rs`)

### Summary

An inbound peer can send a `GetNodes` message with an attacker-chosen `listen_port`. The handler unconditionally rewrites the session's `remote_addr` from `Init(attacker_ip:ephemeral)` to `Listen(attacker_ip:arbitrary_port)` and stores it in the peer store without any port validation. The poisoned address is then eligible for propagation to other peers via `Nodes` responses.

### Finding Description

**Step 1 — Inbound session initialization.**

When an inbound peer connects, `SessionState::new()` sets `remote_addr` to `RemoteAddress::Init(session.address)` — the attacker's real IP with their ephemeral source port. [1](#0-0) 

**Step 2 — `GetNodes` handler calls `update_port()` unconditionally.**

When the inbound peer sends `GetNodes { listen_port: Some(arbitrary_port), ... }`, the handler calls `state.remote_addr.update_port(port)` with no validation of the port value. [2](#0-1) 

**Step 3 — `update_port()` replaces the TCP port and promotes to `Listen`.**

`update_port()` replaces the TCP component of the `Init` address with the attacker-supplied `u16` and transitions the variant to `RemoteAddress::Listen(attacker_ip:arbitrary_port)`. Any port 0–65535 is accepted. [3](#0-2) 

**Step 4 — The `Listen` address is unconditionally passed to `add_new_addr()`.**

Immediately after `update_port()`, the code checks `if let RemoteAddress::Listen(ref addr) = state.remote_addr` — which is now always true — and calls `add_new_addr()`. [4](#0-3) 

**Step 5 — `is_valid_addr()` only checks IP reachability, not the port.**

The sole guard before writing to the peer store checks `is_reachable(socket_addr.ip())` — a public IP check. The port is never inspected. [5](#0-4) 

**Step 6 — `peer_store.add_addr()` has no port validation either.**

`add_addr()` only checks the ban list before storing the address. [6](#0-5) 

**Step 7 — The poisoned address is propagated to other peers.**

`get_random()` fetches from the peer store and returns addresses in `Nodes` responses to other peers, spreading the poisoned `attacker_ip:arbitrary_port` across the network. [7](#0-6) 

### Impact Explanation

The attacker (with a public IP) can inject `their_ip:any_port` (including privileged ports 1–1023) into the peer store of every node they connect to. Those nodes propagate the address to up to 2,500 peers per `Nodes` response. Honest nodes that receive the poisoned address will attempt connections to the wrong port on the attacker's IP, wasting connection slots and degrading peer discovery quality. The attacker can also make themselves effectively undiscoverable at their real port while remaining connected to a subset of nodes.

**Scope constraint:** The attacker can only substitute the port on their own IP — they cannot inject a completely arbitrary IP. The impact is therefore scoped to disruption of connectivity to the attacker's IP across the network, not arbitrary IP injection.

### Likelihood Explanation

This requires only a standard inbound TCP connection from a public IP and a single crafted `GetNodes` message. No authentication, no PoW, no special role. Any node that accepts inbound connections is vulnerable. The `DuplicateGetNodes` guard only prevents sending the message twice per session; a new connection resets the state. [8](#0-7) 

### Recommendation

In `update_port()` or in the `GetNodes` handler before calling `update_port()`, validate that the supplied port is within the expected range for a CKB listen port (e.g., reject port 0 and optionally privileged ports < 1024). Additionally, `is_valid_addr()` should validate the port component, not just the IP.

### Proof of Concept

1. Establish an inbound TCP connection to a victim CKB node from a public IP.
2. Complete the p2p handshake and open the discovery protocol substream.
3. Send a `GetNodes` message with `listen_port = Some(80)`.
4. Query the victim's peer store: assert it contains `your_public_ip:80`.
5. Connect a second node to the victim and receive a `Nodes` response: assert `your_public_ip:80` appears in the response.

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

**File:** network/src/protocols/discovery/mod.rs (L110-115)
```rust
                            if state.received_get_nodes && check(Misbehavior::DuplicateGetNodes) {
                                if context.disconnect(session.id).await.is_err() {
                                    debug!("Disconnect {:?} msg failed to send", session.id)
                                }
                                return;
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

**File:** network/src/protocols/discovery/mod.rs (L375-391)
```rust
    fn get_random(&mut self, n: usize, flags: Flags) -> Vec<(Multiaddr, Flags)> {
        let fetch_random_addrs = self
            .network_state
            .with_peer_store_mut(|peer_store| peer_store.fetch_random_addrs(n, flags));
        let addrs = fetch_random_addrs
            .into_iter()
            .filter_map(|paddr| {
                if !self.is_valid_addr(&paddr.addr) {
                    return None;
                }
                let f = Flags::from_bits_truncate(paddr.flags);
                Some((paddr.addr, f))
            })
            .collect();
        trace!("Discovered random addrs: {:?}", addrs);
        addrs
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

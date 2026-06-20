I have all the code needed to make a determination. Let me trace the full attack path precisely.

### Title
Unauthenticated Inbound Peer Can Inject Arbitrary Port into Peer Store via Spoofed `listen_port` in `GetNodes` — (`network/src/protocols/discovery/mod.rs`, `network/src/protocols/discovery/state.rs`)

---

### Summary

An unprivileged inbound peer can send a crafted `GetNodes` message with an attacker-chosen `listen_port` value. The victim node unconditionally calls `update_port(port)` on the session's `RemoteAddress::Init` state, replacing the TCP port with the attacker-supplied value, then stores the resulting `real_ip:attacker_port` address in the peer store with no port validation at any layer. The poisoned address is subsequently eligible for propagation to other peers via `Nodes` announcements.

---

### Finding Description

**Step 1 — Inbound session initialization**

When an inbound TCP session is established, `SessionState::new()` initializes `remote_addr` as `RemoteAddress::Init(context.session.address.clone())` — the peer's actual ephemeral connection address. [1](#0-0) 

For outbound sessions, `remote_addr` is initialized as `RemoteAddress::Listen(...)` instead, making `update_port` a no-op for them. The vulnerability is exclusive to inbound sessions.

**Step 2 — Unvalidated `GetNodes` processing**

In `received()`, when a `GetNodes` message arrives with a non-`None` `listen_port`, the code calls `update_port(port)` with no check on session direction and no validation of the port value: [2](#0-1) 

**Step 3 — `update_port` accepts any `u16` without bounds checking**

`update_port` only guards on the `RemoteAddress::Init` variant (which inbound sessions have), then blindly substitutes the TCP port component with the attacker-supplied value: [3](#0-2) 

After this call, `remote_addr` transitions to `RemoteAddress::Listen(real_ip:attacker_port)`.

**Step 4 — Address is stored in peer store**

The `if let RemoteAddress::Listen(ref addr)` branch immediately fires (since `update_port` just set it), and `add_new_addr` is called with the poisoned address: [4](#0-3) 

**Step 5 — No port validation in `is_valid_addr` or `add_addr`**

`is_valid_addr` only checks whether the IP is publicly reachable — it never inspects the port: [5](#0-4) 

`add_addr` in `PeerStore` only checks the ban list, then inserts unconditionally: [6](#0-5) 

---

### Impact Explanation

The attacker can cause the victim's peer store to contain `attacker_real_ip:N` for any port `N` in `[0, 65535]`, including port 0 (invalid), port 1 (privileged), or any port that is not actually listening. Because `fetch_random_addrs` deduplicates only by IP (not by IP:port), a single attacker IP can only inject one poisoned entry. However:

- The poisoned entry is eligible for propagation to up to 3 other peers per `ANNOUNCE_INTERVAL` tick via the `notify()` path, spreading the bad address across the network.
- Nodes that receive the address and attempt to connect to `attacker_ip:spoofed_port` will fail, wasting connection slots and degrading outbound connection quality.
- With multiple attacker-controlled IPs (each establishing one inbound connection), the peer store can be systematically filled with unreachable entries up to `ADDR_COUNT_LIMIT = 16384`. [7](#0-6) 

---

### Likelihood Explanation

The attack requires only an inbound TCP connection to the victim — no authentication, no PoW, no privileged role. The `GetNodes` message is a standard discovery protocol message. The `received_get_nodes` flag prevents a second `GetNodes` from the same session, but the attacker only needs one message per connection. The attacker can open many connections from different IPs to scale the attack. [8](#0-7) 

---

### Recommendation

1. **Validate the port value** in `update_port` or at the `received()` call site: reject port 0 and optionally reject well-known privileged ports (< 1024).
2. **Verify session direction** before processing `listen_port`: only inbound sessions should have their port updated via `GetNodes`; outbound sessions already have a verified listen address.
3. **Add port validation in `is_valid_addr`**: reject addresses with port 0 at minimum.
4. **Rate-limit or score** peers that supply ports inconsistent with their connection behavior.

---

### Proof of Concept

```
1. Attacker opens an inbound TCP connection to victim (real IP: 1.2.3.4, ephemeral port: 54321).
2. Victim initializes: remote_addr = RemoteAddress::Init("/ip4/1.2.3.4/tcp/54321/p2p/...")
3. Attacker sends: DiscoveryMessage::GetNodes { listen_port: Some(1), count: 1, version: 0, required_flags: 0 }
4. Victim calls: state.remote_addr.update_port(1)
   → remote_addr becomes RemoteAddress::Listen("/ip4/1.2.3.4/tcp/1/p2p/...")
5. Victim calls: addr_mgr.add_new_addr(session_id, ("/ip4/1.2.3.4/tcp/1/p2p/...", flags))
6. is_valid_addr("/ip4/1.2.3.4/tcp/1/...") → true (IP 1.2.3.4 is reachable)
7. peer_store.add_addr("/ip4/1.2.3.4/tcp/1/p2p/...", flags) → stored
8. On next notify() tick, victim may propagate "/ip4/1.2.3.4/tcp/1/p2p/..." to up to 3 other peers.
``` [3](#0-2) [2](#0-1)

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

**File:** network/src/peer_store/mod.rs (L26-26)
```rust
pub(crate) const ADDR_COUNT_LIMIT: usize = 16384;
```

### Title
Attacker-Controlled DNS Multiaddresses Bypass `is_reachable()` Filter, Enabling SSRF-Analogous Outbound TCP Connections to Internal Hosts — (File: `network/src/protocols/discovery/mod.rs`, `network/src/protocols/identify/mod.rs`)

---

### Summary

Both the Discovery and Identify P2P protocols filter peer-advertised addresses using `is_reachable()` to block private/internal IPs. However, the filter contains a `None => true` branch that unconditionally passes any multiaddress that cannot be converted to a `SocketAddr` at parse time — including DNS-based multiaddresses such as `/dns4/internal.corp/tcp/8080`. These addresses are stored in the peer store and later dialed by `OutboundPeerService`, causing the CKB node to make outbound TCP connections to attacker-specified hosts, including internal network resources.

---

### Finding Description

**Root cause — Discovery protocol (`is_valid_addr`):**

```rust
// network/src/protocols/discovery/mod.rs, lines 332–341
fn is_valid_addr(&self, addr: &Multiaddr) -> bool {
    if !self.discovery_local_address {
        match multiaddr_to_socketaddr(addr) {
            Some(socket_addr) => is_reachable(socket_addr.ip()),
            None => true,   // ← DNS/onion addrs bypass the IP check entirely
        }
    } else {
        true
    }
}
``` [1](#0-0) 

`multiaddr_to_socketaddr` returns `None` for any multiaddr that is not a literal IP (e.g., `/dns4/…`, `/dns6/…`, `/dnsaddr/…`). The `None => true` arm lets these addresses pass without any reachability check. They are then unconditionally inserted into the peer store:

```rust
// lines 347–362
fn add_new_addrs(&mut self, _session_id: SessionId, addrs: Vec<(Multiaddr, Flags)>) {
    for (addr, flags) in addrs.into_iter().filter(|addr| self.is_valid_addr(&addr.0)) {
        self.network_state.with_peer_store_mut(|peer_store| {
            peer_store.add_addr(addr.clone(), flags)
        });
    }
}
``` [2](#0-1) 

**Root cause — Identify protocol (`process_listens`):**

The same `None => true` bypass exists in `IdentifyProtocol::process_listens`:

```rust
// network/src/protocols/identify/mod.rs, lines 139–145
let reachable_addrs = listens
    .into_iter()
    .filter(|addr| match multiaddr_to_socketaddr(addr) {
        Some(socket_addr) => !global_ip_only || is_reachable(socket_addr.ip()),
        None => true,   // ← DNS addrs bypass is_reachable()
    })
    .collect::<Vec<_>>();
self.callback.add_remote_listen_addrs(session, reachable_addrs);
``` [3](#0-2) 

`IdentifyCallback::add_remote_listen_addrs` then stores these addresses in the peer store with no further IP validation:

```rust
// lines 488–494
self.network_state.with_peer_store_mut(|peer_store| {
    for addr in addrs {
        peer_store.add_addr(addr.clone(), flags)
    }
})
``` [4](#0-3) 

**Dial path — `OutboundPeerService`:**

`OutboundPeerService::try_dial_peers` fetches addresses from the peer store and dials them without any additional IP-range check:

```rust
// network/src/services/outbound_peer.rs, lines 174–183
for mut addr in peers {
    self.network_state.dial_identify(&self.p2p_control, addr);
}
``` [5](#0-4) 

`dial_inner` / `can_dial` only checks for self-connection and duplicate dials — no private-range filter:

```rust
// network/src/network.rs, lines 392–446
pub(crate) fn can_dial(&self, addr: &Multiaddr) -> bool {
    // checks: peer_id present, not self, not in registry, not already dialing
    // NO is_reachable() check
}
``` [6](#0-5) 

---

### Impact Explanation

An unprivileged peer that connects to a CKB node can send a crafted `Nodes` (Discovery) or `IdentifyMessage` (Identify) containing DNS-based multiaddresses such as:

```
/dns4/169.254.169.254.attacker.com/tcp/80/p2p/<valid_peer_id>
/dns4/internal-k8s-api.corp/tcp/6443/p2p/<valid_peer_id>
```

Where the DNS name resolves to an internal IP at dial time. The CKB node will:
1. Store the address in its peer store (bypassing `is_reachable()`).
2. Attempt a TCP connection to the resolved internal IP via `dial_identify`.
3. Perform a secio/noise handshake attempt, which constitutes a full TCP connection to the internal host.

This enables:
- **Internal port scanning**: connection success/failure reveals open ports on internal hosts.
- **Triggering internal services**: any service that reacts to incoming TCP connections (e.g., cloud metadata endpoints, internal APIs) can be reached from the CKB node's network position.
- **Escalation on cloud deployments**: on AWS/GCP/Azure, the instance metadata endpoint (`169.254.169.254`) is reachable via TCP; a successful connection attempt may leak timing information or trigger rate-limiting behavior observable by the attacker.

The impact is structurally identical to the reported SSRF: an unprivileged external party causes the server to make outbound network connections to attacker-specified internal addresses.

---

### Likelihood Explanation

- The Discovery and Identify protocols are enabled by default and accept messages from any connected peer.
- Any node on the CKB network can connect as an inbound peer (no authentication required before protocol messages are exchanged).
- Crafting a valid `Nodes` or `IdentifyMessage` with DNS multiaddresses requires only knowledge of the wire format, which is publicly documented.
- The `OutboundPeerService` runs on a timer and will automatically attempt to dial stored addresses, so no further interaction is needed after injecting the address.

Likelihood: **Medium** (trivially reachable by any peer; impact depends on deployment environment).

---

### Recommendation

Replace the `None => true` fallback in both `is_valid_addr` and `process_listens` with an explicit allowlist of non-IP multiaddr protocols that are considered safe (e.g., `/onion3/`), and reject all others including `/dns4/`, `/dns6/`, and `/dnsaddr/` unless the resolved IP is verified to be globally reachable. Alternatively, perform a DNS pre-resolution with an `is_reachable()` check on the result before storing the address in the peer store.

---

### Proof of Concept

1. Connect to a target CKB node as a peer (standard P2P connection).
2. After the Identify handshake, send a `Nodes` Discovery message containing:
   ```
   /dns4/<attacker-controlled-domain>/tcp/8080/p2p/<any_valid_peer_id>
   ```
   where `<attacker-controlled-domain>` has a DNS A record pointing to `10.0.0.1` (or any internal IP).
3. Observe (via DNS query logs on the attacker's nameserver, or via a listener on the internal IP) that the CKB node resolves the DNS name and initiates a TCP connection to the internal address within the next `connect_outbound_interval_secs` (default: 15 seconds).
4. Repeat with different internal IPs/ports to enumerate the internal network topology.

### Citations

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

**File:** network/src/protocols/discovery/mod.rs (L347-362)
```rust
    fn add_new_addrs(&mut self, _session_id: SessionId, addrs: Vec<(Multiaddr, Flags)>) {
        if addrs.is_empty() {
            return;
        }

        for (addr, flags) in addrs.into_iter().filter(|addr| self.is_valid_addr(&addr.0)) {
            trace!("Add discovered address:{:?}", addr);
            self.network_state.with_peer_store_mut(|peer_store| {
                if let Err(err) = peer_store.add_addr(addr.clone(), flags) {
                    debug!(
                        "Failed to add discovered address to peer_store {:?} {:?}",
                        err, addr
                    );
                }
            });
        }
```

**File:** network/src/protocols/identify/mod.rs (L138-148)
```rust
            let global_ip_only = self.global_ip_only;
            let reachable_addrs = listens
                .into_iter()
                .filter(|addr| match multiaddr_to_socketaddr(addr) {
                    Some(socket_addr) => !global_ip_only || is_reachable(socket_addr.ip()),
                    None => true,
                })
                .collect::<Vec<_>>();
            self.callback
                .add_remote_listen_addrs(session, reachable_addrs);
            MisbehaveResult::Continue
```

**File:** network/src/protocols/identify/mod.rs (L488-494)
```rust
        self.network_state.with_peer_store_mut(|peer_store| {
            for addr in addrs {
                if let Err(err) = peer_store.add_addr(addr.clone(), flags) {
                    error!("IdentifyProtocol failed to add address to peer store, address: {}, error: {:?}", addr, err);
                }
            }
        })
```

**File:** network/src/services/outbound_peer.rs (L174-183)
```rust
        for mut addr in peers {
            self.network_state.dial_identify(&self.p2p_control, {
                match &self.transport_type {
                    TransportType::Tcp => (),
                    TransportType::Ws => addr.push(Protocol::Ws),
                    TransportType::Wss => addr.push(Protocol::Wss),
                }
                addr
            });
        }
```

**File:** network/src/network.rs (L392-446)
```rust
    pub(crate) fn can_dial(&self, addr: &Multiaddr) -> bool {
        let peer_id = extract_peer_id(addr);
        if peer_id.is_none() {
            error!("Do not dial addr without peer id, addr: {}", addr);
            return false;
        }
        let peer_id = peer_id.as_ref().unwrap();

        if self.local_peer_id() == peer_id {
            trace!("Do not dial self: {:?}, {}", peer_id, addr);
            return false;
        }
        if self.public_addrs.read().contains(addr) {
            trace!(
                "Do not dial listened address(self): {:?}, {}",
                peer_id, addr
            );
            return false;
        }

        let peer_in_registry = self.with_peer_registry(|reg| {
            reg.get_key_by_peer_id(peer_id).is_some() || reg.is_feeler(addr)
        });
        if peer_in_registry {
            trace!("Do not dial peer in registry: {:?}, {}", peer_id, addr);
            return false;
        }

        if let Some(dial_started) = self.dialing_addrs.read().get(peer_id) {
            trace!(
                "Do not send repeated dial commands to network service: {:?}, {}",
                peer_id, addr
            );
            if Instant::now().saturating_duration_since(*dial_started) > DIAL_HANG_TIMEOUT {
                #[cfg(feature = "with_sentry")]
                with_scope(
                    |scope| scope.set_fingerprint(Some(&["ckb-network", "dialing-timeout"])),
                    || {
                        capture_message(
                            &format!(
                                "Dialing {:?}, {:?} for more than {} seconds, \
                                 something is wrong in network service",
                                peer_id,
                                addr,
                                DIAL_HANG_TIMEOUT.as_secs(),
                            ),
                            Level::Warning,
                        )
                    },
                );
            }
            return false;
        }

        true
```

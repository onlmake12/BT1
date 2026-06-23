### Title
Non-IP Multiaddr Bypass in `process_listens` Allows Peer Store Pollution — (`network/src/protocols/identify/mod.rs`)

### Summary

The `process_listens` filter in `IdentifyProtocol` uses `None => true` as the fallback for addresses where `multiaddr_to_socketaddr` returns `None`. This means any Multiaddr that does not resolve to a socket address (e.g., `/dns4/attacker.com/tcp/80`, `/memory/1234`) unconditionally passes the `global_ip_only` guard and is written into the peer store via `add_remote_listen_addrs` → `peer_store.add_addr`.

---

### Finding Description

**Filter logic in `process_listens`:** [1](#0-0) 

```rust
let reachable_addrs = listens
    .into_iter()
    .filter(|addr| match multiaddr_to_socketaddr(addr) {
        Some(socket_addr) => !global_ip_only || is_reachable(socket_addr.ip()),
        None => true,   // ← non-IP addrs pass unconditionally
    })
    .collect::<Vec<_>>();
```

When `global_ip_only = true` (the hardcoded production default), the intent is to admit only globally routable IP addresses. But the `None` arm admits everything that is not an IP-based multiaddr — including `/dns4/`, `/dns6/`, `/memory/`, and arbitrary unknown protocol stacks — because `multiaddr_to_socketaddr` returns `None` for all of them.

**`global_ip_only` is always `true` in production:** [2](#0-1) 

The `global_ip_only(false)` setter is `#[cfg(test)]`-gated, so production nodes always run with the flag set.

**`add_remote_listen_addrs` writes directly to the peer store without further filtering:** [3](#0-2) 

```rust
self.network_state.with_peer_store_mut(|peer_store| {
    for addr in addrs {
        if let Err(err) = peer_store.add_addr(addr.clone(), flags) { ... }
    }
})
```

**`add_addr` performs only a ban-list check:** [4](#0-3) 

No address-family validation occurs here.

**Contrast with the local-send path**, which correctly restricts non-IP addresses to Onion3 only: [5](#0-4) 

```rust
if let Some(socket_addr) = multiaddr_to_socketaddr(addr) {
    !self.global_ip_only || is_reachable(socket_addr.ip())
} else {
    // allow /onion3 address
    addr.iter().any(|protocol| matches!(protocol, Protocol::Onion3(_)))
}
```

The outbound (local) filter explicitly restricts the `None` case to Onion3. The inbound (`process_listens`) filter does not — it uses the blanket `None => true`.

---

### Impact Explanation

An unprivileged remote peer that completes a valid identify handshake (correct network name, non-zero flags) can inject up to `MAX_ADDRS = 10` arbitrary non-IP multiaddrs per connection into the victim node's peer store. [6](#0-5) 

Injected addresses are stored with `last_connected_at_ms = 0`. The `fetch_random` path in `AddrManager` does apply a secondary `is_connectable` check for non-IP addresses, which prevents immediate re-broadcast via the discovery protocol: [7](#0-6) 

However, the peer store is still polluted: slots are consumed, and `fetch_addrs_to_feeler` / `fetch_addrs_to_attempt` paths (which have separate logic) may attempt outbound connections to attacker-controlled hostnames, leaking the node's identity and wasting resources. The discovery `is_valid_addr` also uses `None => true`: [8](#0-7) 

meaning that if any of these addresses later become "connectable" (e.g., via a separate injection that sets `last_connected_at_ms`), they would pass the discovery broadcast filter.

---

### Likelihood Explanation

Any peer that can complete a TCP handshake and pass the identify network-name check can trigger this. No special privileges, keys, or majority hashpower are required. The attacker only needs to send a valid `IdentifyMessage` with DNS or other non-IP multiaddrs in `listen_addrs`.

---

### Recommendation

Replace the `None => true` fallback in `process_listens` with the same Onion3-only allowlist already used in the local-send path:

```rust
.filter(|addr| match multiaddr_to_socketaddr(addr) {
    Some(socket_addr) => !global_ip_only || is_reachable(socket_addr.ip()),
    None => addr.iter().any(|p| matches!(p, Protocol::Onion3(_))),
})
``` [1](#0-0) 

---

### Proof of Concept

```rust
// Construct a /dns4/ multiaddr — multiaddr_to_socketaddr returns None for this
let dns_addr: Multiaddr = "/dns4/attacker.com/tcp/8115".parse().unwrap();
assert!(multiaddr_to_socketaddr(&dns_addr).is_none());

// process_listens with global_ip_only=true still passes it through
// because None => true
let listens = vec![dns_addr.clone()];
// After process_listens, add_remote_listen_addrs is called with [dns_addr]
// peer_store.add_addr(dns_addr, flags) succeeds — no IP validation at that layer

// Verify it is now in the peer store
assert!(peer_store.addr_manager().get(&dns_addr).is_some());
```

### Citations

**File:** network/src/protocols/identify/mod.rs (L30-30)
```rust
const MAX_ADDRS: usize = 10;
```

**File:** network/src/protocols/identify/mod.rs (L93-99)
```rust
    pub fn new(callback: T) -> IdentifyProtocol<T> {
        IdentifyProtocol {
            callback,
            remote_infos: HashMap::default(),
            global_ip_only: true,
        }
    }
```

**File:** network/src/protocols/identify/mod.rs (L139-145)
```rust
            let reachable_addrs = listens
                .into_iter()
                .filter(|addr| match multiaddr_to_socketaddr(addr) {
                    Some(socket_addr) => !global_ip_only || is_reachable(socket_addr.ip()),
                    None => true,
                })
                .collect::<Vec<_>>();
```

**File:** network/src/protocols/identify/mod.rs (L217-225)
```rust
                .filter(|addr| {
                    if let Some(socket_addr) = multiaddr_to_socketaddr(addr) {
                        !self.global_ip_only || is_reachable(socket_addr.ip())
                    } else {
                        // allow /onion3 address
                        addr.iter()
                            .any(|protocol| matches!(protocol, Protocol::Onion3(_)))
                    }
                })
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

**File:** network/src/peer_store/addr_manager.rs (L74-89)
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
                        } else {
                            debug!(
                                "addr {:?} is not connectable and not an onion address",
                                addr_info.addr
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

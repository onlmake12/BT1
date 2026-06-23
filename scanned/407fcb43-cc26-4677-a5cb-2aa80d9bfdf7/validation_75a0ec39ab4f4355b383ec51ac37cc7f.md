The code is clear enough to render a verdict. Here is the analysis:

**Confirmed facts from the code:**

1. `BanList::is_addr_banned` at `ban_list.rs:68-72` calls `multiaddr_to_socketaddr(addr)` and uses `.unwrap_or_default()` — for any DNS multiaddr (`/dns4/...` or `/dns6/...`), `multiaddr_to_socketaddr` returns `None`, so the function returns `false` (not banned). [1](#0-0) 

2. `add_outbound_addr` and `add_addr` both gate on `is_addr_banned`, which is always `false` for DNS addresses — so DNS-form addresses of banned IPs pass straight through. [2](#0-1) [3](#0-2) 

3. `test_support_dns_store` explicitly confirms that `/dns4/...` addresses are accepted and stored in the peer store via `add_addr`. [4](#0-3) 

4. `DiscoveryAddressManager::is_valid_addr` passes DNS addresses through (when `multiaddr_to_socketaddr` returns `None`, it returns `true`), so Discovery can inject DNS addresses into the peer store. [5](#0-4) 

5. `OutboundPeerService::dial_feeler` explicitly includes `Dns4`/`Dns6` addresses in its filter for Ws/Wss transport, and for TCP transport the filter is `true` (all addresses pass). [6](#0-5) 

6. `PeerRegistry::accept_peer` also calls `peer_store.is_addr_banned(&remote_addr)` — which again returns `false` for DNS addresses — so the banned peer is accepted at the connection level too. [7](#0-6) 

7. `ban_addr` itself only bans the IP extracted via `multiaddr_to_socketaddr`; a DNS alias for the same IP is never added to the ban list. [8](#0-7) 

**Attack path assessment:**

The path is concrete: a peer advertises `/dns4/attacker.com/tcp/8114/p2p/<PEERID>` via Discovery → victim stores it → victim's IP is banned → DNS address bypasses the ban check at every gate (`add_addr`, `add_outbound_addr`, `accept_peer`) → victim dials and accepts the banned peer again. No privileged access, no majority hashpower, no social engineering required.

**However**, the impact ceiling matters. Ban evasion means a misbehaving peer can reconnect after being banned. This is a real bypass of an explicit security mechanism. But:
- It does not directly break consensus (the attacker still has to win the normal protocol-level checks for blocks/transactions).
- "Consensus disruption" would require additionally eclipsing the victim, which needs filling all outbound slots — a much harder bar.
- The practical impact is persistent reconnection by a banned peer, resource waste, and potential for sustained harassment, not direct consensus compromise.

The ban bypass is real and code-confirmed. The "consensus disruption" framing is speculative and not directly reachable from this path alone.

---

### Title
DNS Multiaddr Bypasses IP-Based Ban Check, Allowing Banned Peers to Re-enter Peer Store — (`network/src/peer_store/ban_list.rs`)

### Summary
`BanList::is_addr_banned` unconditionally returns `false` for any multiaddr that cannot be converted to a socket address (i.e., all DNS-form addresses). Because the Discovery protocol accepts and stores DNS multiaddrs, and the feeler/identify dial loops will dial them, a banned peer can re-enter the peer store and be re-accepted by advertising a DNS alias for its banned IP.

### Finding Description
`is_addr_banned` in `ban_list.rs:68-72` calls `multiaddr_to_socketaddr(addr)` and falls back to `unwrap_or_default()` (i.e., `false`) when the conversion fails. For `/dns4/` or `/dns6/` multiaddrs, `multiaddr_to_socketaddr` always returns `None`, so the ban check is silently skipped. This same function is the sole ban gate in `add_addr`, `add_outbound_addr`, `update_outbound_addr_last_connected_ms`, and `PeerRegistry::accept_peer`. `ban_addr` itself only records the IP extracted from the multiaddr, so a DNS alias for the same IP is never added to the ban list. The `test_support_dns_store` test confirms DNS addresses are stored without restriction, and `DiscoveryAddressManager::is_valid_addr` explicitly passes DNS addresses through.

### Impact Explanation
A banned peer can re-enter the peer store and be re-accepted as an outbound connection by advertising a DNS alias. The ban mechanism — intended to prevent reconnection by misbehaving peers — is fully ineffective against peers that control a DNS name. The practical impact is persistent reconnection after banning, sustained resource consumption, and potential for harassment. Direct consensus disruption requires additionally eclipsing the victim node, which is a higher bar not directly reachable from this path alone.

### Likelihood Explanation
Any peer that controls a DNS hostname pointing to their IP can exploit this. The attacker only needs to have their DNS address propagated into the victim's peer store via Discovery before or after the ban — a normal P2P operation. No special privileges are required.

### Recommendation
In `is_addr_banned`, when `multiaddr_to_socketaddr` returns `None`, do not default to "not banned." Instead, attempt DNS resolution at ban-check time (or at store-time), or maintain a separate DNS-hostname ban list. Alternatively, resolve DNS addresses to IPs before storing them in the peer store, so all stored addresses are in IP form and the existing IP-based ban check applies uniformly.

### Proof of Concept
```rust
// 1. Ban IP 1.2.3.4
peer_store.ban_addr(&"/ip4/1.2.3.4/tcp/8114/p2p/PEERID".parse().unwrap(), 999_999_999, "test".into());

// 2. Advertise DNS alias for the same IP via Discovery
let dns_addr: Multiaddr = "/dns4/attacker.com/tcp/8114/p2p/PEERID".parse().unwrap();
peer_store.add_addr(dns_addr.clone(), Flags::COMPATIBILITY).unwrap();

// 3. Assert: DNS address is stored despite the IP being banned
assert!(peer_store.addr_manager().get(&dns_addr).is_some()); // passes — ban was bypassed

// 4. Assert: add_outbound_addr also bypasses the ban
peer_store.add_outbound_addr(dns_addr.clone(), Flags::COMPATIBILITY);
assert!(peer_store.addr_manager().get(&dns_addr).is_some()); // passes
```

### Citations

**File:** network/src/peer_store/ban_list.rs (L68-72)
```rust
    pub fn is_addr_banned(&self, addr: &Multiaddr) -> bool {
        multiaddr_to_socketaddr(addr)
            .map(|socket_addr| self.is_ip_banned(&socket_addr.ip()))
            .unwrap_or_default()
    }
```

**File:** network/src/peer_store/peer_store_impl.rs (L71-79)
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
```

**File:** network/src/peer_store/peer_store_impl.rs (L103-114)
```rust
    pub fn add_outbound_addr(&mut self, addr: Multiaddr, flags: Flags) {
        if self.ban_list.is_addr_banned(&addr) {
            return;
        }
        let score = self.score_config.default_score;
        self.addr_manager.add(AddrInfo::new(
            addr,
            ckb_systemtime::unix_time_as_millis(),
            score,
            flags.bits(),
        ));
    }
```

**File:** network/src/peer_store/peer_store_impl.rs (L286-292)
```rust
    pub(crate) fn ban_addr(&mut self, addr: &Multiaddr, timeout_ms: u64, ban_reason: String) {
        if let Some(addr) = multiaddr_to_socketaddr(addr) {
            let network = ip_to_network(addr.ip());
            self.ban_network(network, timeout_ms, ban_reason)
        }
        self.addr_manager.remove(addr);
    }
```

**File:** network/src/tests/peer_store.rs (L626-642)
```rust
#[test]
fn test_support_dns_store() {
    let mut peer_store = PeerStore::default();
    let addr: Multiaddr = format!(
        "/dns4/www.abc.com/tcp/{}/p2p/{}",
        rand::random::<u16>(),
        crate::PeerId::random().to_base58()
    )
    .parse()
    .unwrap();

    peer_store
        .add_addr(addr.clone(), Flags::COMPATIBILITY)
        .unwrap();
    assert_eq!(peer_store.fetch_addrs_to_feeler(2, |_| true).len(), 1);
    assert_eq!(peer_store.fetch_addrs_to_feeler(1, |_| true)[0].addr, addr);
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

**File:** network/src/services/outbound_peer.rs (L56-68)
```rust
    fn dial_feeler(&mut self) {
        let now_ms = unix_time_as_millis();
        let filter = |peer_addr: &AddrInfo| match self.transport_type {
            TransportType::Tcp => true,
            TransportType::Ws => peer_addr
                .addr
                .iter()
                .any(|p| matches!(p, Protocol::Dns4(_) | Protocol::Dns6(_) | Protocol::Tcp(_))),
            TransportType::Wss => peer_addr
                .addr
                .iter()
                .any(|p| matches!(p, Protocol::Dns4(_) | Protocol::Dns6(_))),
        };
```

**File:** network/src/peer_registry.rs (L109-111)
```rust
            if peer_store.is_addr_banned(&remote_addr) {
                return Err(PeerError::Banned.into());
            }
```

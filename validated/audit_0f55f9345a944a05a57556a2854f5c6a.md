### Title
IPv4-Mapped IPv6 Address Aliasing Bypasses Peer Ban List - (`File: network/src/peer_store/ban_list.rs`)

### Summary

The `BanList::is_ip_banned_until` function does not normalize IPv4-mapped IPv6 addresses (e.g., `::ffff:192.168.0.1`) to their IPv4 equivalents before checking the ban list. A peer banned under its IPv4 address can immediately reconnect using the IPv4-mapped IPv6 form of the same address and bypass the ban entirely. The inverse is also true.

### Finding Description

When a peer is banned, `ban_addr` extracts the socket IP and stores it via `ip_to_network`, which preserves the address family verbatim: [1](#0-0) 

So a peer connected as `/ip4/1.2.3.4/tcp/...` is stored as `IpNetwork::V4(1.2.3.4/32)`.

When a new connection arrives, `accept_peer` calls `peer_store.is_addr_banned(&remote_addr)`: [2](#0-1) 

This reaches `BanList::is_ip_banned_until`, which converts the incoming IP to a network key and then iterates the ban list: [3](#0-2) 

The `ipnetwork` crate's `IpNetwork::contains` returns `false` for cross-family comparisons — `IpNetwork::V4(1.2.3.4/32).contains(IpAddr::V6(::ffff:1.2.3.4))` is always `false`. Therefore, if the banned peer reconnects presenting `/ip6/::ffff:1.2.3.4/tcp/...`, the lookup at line 50 misses (wrong key family), and the `contains` check at line 57 also returns `false`. The peer is accepted.

The contrast with `network_group.rs` is telling: the `Group` implementation explicitly normalizes IPv4-mapped IPv6 addresses to `Group::IP4` for peer diversity accounting, demonstrating developer awareness of the aliasing issue — but this normalization was never applied to the ban list: [4](#0-3) 

### Impact Explanation

A peer banned for misbehavior (e.g., sending malformed messages, protocol violations, or triggering score-based bans) can immediately reconnect using the IPv4-mapped IPv6 form of its IP address and bypass the ban. This nullifies the node's primary defense against actively misbehaving peers. Repeated malformed-message attacks, resource exhaustion, or sync disruption attacks that rely on being banned to stop become ineffective as a defense.

### Likelihood Explanation

The attacker is an unprivileged peer with no special access. IPv4-mapped IPv6 is supported on all major operating systems. A peer that has been banned has a direct incentive to reconnect. The bypass requires only changing the address format in the multiaddr used to connect — no cryptographic material, no new IP address, no privileged access needed.

### Recommendation

In `is_ip_banned_until`, normalize the incoming `IpAddr` before the lookup: if the address is `IpAddr::V6` and `ipv6.to_ipv4()` returns `Some(v4)`, use `IpAddr::V4(v4)` for the check. Apply the same normalization in `ip_to_network` or add a dedicated helper. Mirror the logic already present in `network_group.rs`:

```rust
fn normalize_ip(ip: IpAddr) -> IpAddr {
    if let IpAddr::V6(v6) = ip {
        if let Some(v4) = v6.to_ipv4() {
            return IpAddr::V4(v4);
        }
    }
    ip
}
```

Call `normalize_ip(ip)` at the top of `is_ip_banned_until` before any lookup.

### Proof of Concept

1. Node A bans peer B (IPv4 `1.2.3.4`) for sending a malformed message. `ban_addr` stores `IpNetwork::V4(1.2.3.4/32)` in the ban list.
2. Peer B reconnects immediately using multiaddr `/ip6/::ffff:1.2.3.4/tcp/<port>/p2p/<peer_id>`.
3. `accept_peer` calls `is_addr_banned`. `multiaddr_to_socketaddr` returns `SocketAddr::V6([::ffff:1.2.3.4]:port)`. `socket_addr.ip()` returns `IpAddr::V6(::ffff:1.2.3.4)`.
4. `ip_to_network` produces `IpNetwork::V6(::ffff:1.2.3.4/128)`. The `inner.get` lookup finds no entry (key family mismatch). The `iter().any(|net| net.contains(ip))` check evaluates `IpNetwork::V4(1.2.3.4/32).contains(IpAddr::V6(::ffff:1.2.3.4))` → `false`.
5. `is_addr_banned` returns `false`. Peer B is accepted despite being banned. [5](#0-4) [6](#0-5) [7](#0-6)

### Citations

**File:** network/src/peer_store/types.rs (L151-156)
```rust
/// Convert IpAddr to IpNetwork
pub fn ip_to_network(ip: IpAddr) -> IpNetwork {
    match ip {
        IpAddr::V4(ipv4) => IpNetwork::V4(ipv4.into()),
        IpAddr::V6(ipv6) => IpNetwork::V6(ipv6.into()),
    }
```

**File:** network/src/peer_registry.rs (L101-111)
```rust
        let is_whitelist = self.whitelist_peers.contains(&peer_id);
        let mut evicted_peer: Option<Peer> = None;

        let mut session_type: SessionType = raw_session_type.into();
        if !is_whitelist {
            if self.whitelist_only {
                return Err(PeerError::NonReserved.into());
            }
            if peer_store.is_addr_banned(&remote_addr) {
                return Err(PeerError::Banned.into());
            }
```

**File:** network/src/peer_store/ban_list.rs (L48-72)
```rust
    fn is_ip_banned_until(&self, ip: IpAddr, now_ms: u64) -> bool {
        let ip_network = ip_to_network(ip);
        if let Some(banned_addr) = self.inner.get(&ip_network)
            && banned_addr.ban_until.gt(&now_ms)
        {
            return true;
        }

        self.inner.iter().any(|(ip_network, banned_addr)| {
            banned_addr.ban_until.gt(&now_ms) && ip_network.contains(ip)
        })
    }

    /// Whether the ip is banned
    pub fn is_ip_banned(&self, ip: &IpAddr) -> bool {
        let now_ms = unix_time_as_millis();
        self.is_ip_banned_until(ip.to_owned(), now_ms)
    }

    /// Whether the address is banned
    pub fn is_addr_banned(&self, addr: &Multiaddr) -> bool {
        multiaddr_to_socketaddr(addr)
            .map(|socket_addr| self.is_ip_banned(&socket_addr.ip()))
            .unwrap_or_default()
    }
```

**File:** network/src/network_group.rs (L31-35)
```rust
            if let IpAddr::V6(ipv6) = ip_addr {
                if let Some(ipv4) = ipv6.to_ipv4() {
                    let bits = ipv4.octets();
                    return Group::IP4([bits[0], bits[1]]);
                }
```

**File:** network/src/peer_store/peer_store_impl.rs (L285-292)
```rust
    /// Ban an addr
    pub(crate) fn ban_addr(&mut self, addr: &Multiaddr, timeout_ms: u64, ban_reason: String) {
        if let Some(addr) = multiaddr_to_socketaddr(addr) {
            let network = ip_to_network(addr.ip());
            self.ban_network(network, timeout_ms, ban_reason)
        }
        self.addr_manager.remove(addr);
    }
```

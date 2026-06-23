The code path is real and the bypass is technically sound. Key evidence:

**`ip_to_network` has no normalization** — it blindly maps `IpAddr::V6` → `IpNetwork::V6`, so `::ffff:1.2.3.4` becomes `IpNetwork::V6(::ffff:1.2.3.4/128)`, never matching a stored `IpNetwork::V4(1.2.3.4/32)`.

**The linear scan also fails** — `IpNetwork::V4(1.2.3.4/32).contains(IpAddr::V6(::ffff:1.2.3.4))` returns `false` in the `ipnetwork` crate because a V4 network cannot contain a V6 address.

**The codebase is inconsistent** — `network_group.rs` line 32 explicitly calls `ipv6.to_ipv4()` to normalize IPv4-mapped IPv6 addresses for grouping, but `ip_to_network` in `types.rs` performs no such normalization for ban checks.

---

### Title
IPv4-mapped IPv6 Address Bypasses Peer Ban List — (`network/src/peer_store/ban_list.rs`)

### Summary
Banning an IPv4 address stores `IpNetwork::V4(x.x.x.x/32)`. A peer reconnecting via the IPv4-mapped IPv6 form (`::ffff:x.x.x.x`) causes `is_ip_banned_until` to look up `IpNetwork::V6(::ffff:x.x.x.x/128)`, which misses the V4 entry in both the HashMap fast-path and the linear-scan fallback, allowing the banned peer to reconnect.

### Finding Description

`ban_addr` calls `ip_to_network(addr.ip())` which stores the ban as `IpNetwork::V4`: [1](#0-0) 

`ip_to_network` performs no address-family normalization: [2](#0-1) 

When a peer reconnects via `::ffff:1.2.3.4`, `is_ip_banned_until` converts it to `IpNetwork::V6(::ffff:1.2.3.4/128)`. The HashMap lookup misses the V4 entry, and the linear scan's `ip_network.contains(ip)` also returns `false` because a V4 network cannot contain a V6 `IpAddr`: [3](#0-2) 

By contrast, `network_group.rs` correctly normalizes IPv4-mapped IPv6 via `ipv6.to_ipv4()` — demonstrating the codebase is aware of the issue but the ban path was not updated: [4](#0-3) 

### Impact Explanation
A peer banned for misbehavior (e.g., sending invalid blocks/headers, triggering `ban_session`) can immediately reconnect by dialing the node's IPv6 endpoint using the IPv4-mapped form of its banned IPv4 address. The ban mechanism is fully bypassed for that peer, allowing continued misbehavior, connection-slot consumption, and repeated triggering of any protocol-level abuse that caused the ban.

### Likelihood Explanation
Exploitability requires the CKB node to accept IPv6 connections (common in dual-stack deployments) and the attacker to know their IPv4 was banned. Both conditions are realistic. The attacker needs no privileges — only the ability to initiate a TCP connection from the same IP via IPv6.

### Recommendation
In `ip_to_network` (or at the call sites in `ban_addr` and `is_ip_banned_until`), normalize IPv4-mapped IPv6 addresses to their IPv4 equivalents before constructing the `IpNetwork`:

```rust
pub fn ip_to_network(ip: IpAddr) -> IpNetwork {
    let ip = match ip {
        IpAddr::V6(v6) => v6.to_ipv4_mapped()
            .map(IpAddr::V4)
            .unwrap_or(IpAddr::V6(v6)),
        v4 => v4,
    };
    match ip {
        IpAddr::V4(ipv4) => IpNetwork::V4(ipv4.into()),
        IpAddr::V6(ipv6) => IpNetwork::V6(ipv6.into()),
    }
}
```

This mirrors the normalization already applied in `network_group.rs`.

### Proof of Concept
```rust
use std::net::{IpAddr, Ipv4Addr, Ipv6Addr};
use ipnetwork::IpNetwork;

// Ban 1.2.3.4 (as happens via ban_addr on an IPv4 peer)
let banned_ip = IpAddr::V4(Ipv4Addr::new(1, 2, 3, 4));
let banned_network = ip_to_network(banned_ip); // IpNetwork::V4(1.2.3.4/32)
ban_list.ban(BannedAddr { address: banned_network, ban_until: u64::MAX, .. });

// Reconnect via IPv4-mapped IPv6
let mapped = IpAddr::V6(Ipv6Addr::new(0, 0, 0, 0, 0, 0xffff, 0x0102, 0x0304));
assert!(!ban_list.is_ip_banned(&mapped)); // returns false — ban bypassed
``` [3](#0-2) [2](#0-1)

### Citations

**File:** network/src/peer_store/peer_store_impl.rs (L287-289)
```rust
        if let Some(addr) = multiaddr_to_socketaddr(addr) {
            let network = ip_to_network(addr.ip());
            self.ban_network(network, timeout_ms, ban_reason)
```

**File:** network/src/peer_store/types.rs (L152-156)
```rust
pub fn ip_to_network(ip: IpAddr) -> IpNetwork {
    match ip {
        IpAddr::V4(ipv4) => IpNetwork::V4(ipv4.into()),
        IpAddr::V6(ipv6) => IpNetwork::V6(ipv6.into()),
    }
```

**File:** network/src/peer_store/ban_list.rs (L48-58)
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
```

**File:** network/src/network_group.rs (L31-35)
```rust
            if let IpAddr::V6(ipv6) = ip_addr {
                if let Some(ipv4) = ipv6.to_ipv4() {
                    let bits = ipv4.octets();
                    return Group::IP4([bits[0], bits[1]]);
                }
```

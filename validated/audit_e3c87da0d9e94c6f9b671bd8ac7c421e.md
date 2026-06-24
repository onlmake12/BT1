All six cited code paths are confirmed against the actual source. Here is the validation result:

---

Audit Report

## Title
IPv4-Mapped IPv6 Address Aliasing Bypasses Peer Ban List - (File: network/src/peer_store/ban_list.rs)

## Summary
`BanList::is_ip_banned_until` converts the incoming `IpAddr` to an `IpNetwork` via `ip_to_network` without normalizing IPv4-mapped IPv6 addresses first. A peer banned under its IPv4 address can immediately reconnect using the IPv4-mapped IPv6 form (`::ffff:x.x.x.x`) of the same address and bypass the ban entirely. The normalization logic already exists in `network_group.rs` but was never applied to the ban list path.

## Finding Description
`ip_to_network` in `network/src/peer_store/types.rs` (L152–156) maps `IpAddr::V4` → `IpNetwork::V4` and `IpAddr::V6` → `IpNetwork::V6` with no normalization. [1](#0-0) 

`ban_addr` in `peer_store_impl.rs` (L286–292) calls `ip_to_network(addr.ip())` and stores the result as the HashMap key, so a ban on `1.2.3.4` is stored as `IpNetwork::V4(1.2.3.4/32)`. [2](#0-1) 

`is_ip_banned_until` (L48–58) first does a direct HashMap lookup using `ip_to_network(ip)` — for `IpAddr::V6(::ffff:1.2.3.4)` this produces `IpNetwork::V6(::ffff:1.2.3.4/128)`, which finds no entry. The fallback `iter().any(|net| net.contains(ip))` then evaluates `IpNetwork::V4(1.2.3.4/32).contains(IpAddr::V6(::ffff:1.2.3.4))`, which the `ipnetwork` crate returns `false` for cross-family comparisons. The function returns `false`. [3](#0-2) 

`accept_peer` in `peer_registry.rs` (L109–111) calls `peer_store.is_addr_banned(&remote_addr)` with no prior normalization, so the bypass is reachable from any inbound connection. [4](#0-3) 

The normalization fix (`ipv6.to_ipv4()`) already exists in `network_group.rs` (L31–35) for peer diversity accounting but was never applied to the ban list. [5](#0-4)  A `grep_search` confirms `to_ipv4` appears only in `network_group.rs`, never in the ban list code. [6](#0-5) 

## Impact Explanation
The ban list is the node's primary runtime defense against actively misbehaving peers. A peer that can bypass it can reconnect indefinitely with zero cost (no new IP, no new key material, no privilege required). This maps to **High: Vulnerabilities or bad designs which could cause CKB network congestion with few costs** — a banned peer can repeatedly reconnect, re-trigger whatever behavior caused the ban (malformed messages, score-based bans, resource exhaustion), and sustain the attack without interruption. The attacker's cost is a single address-family change per reconnect.

## Likelihood Explanation
The attacker is an unprivileged inbound peer. IPv4-mapped IPv6 is supported on all major operating systems and requires no new IP address, no cryptographic material, and no privileged access. A peer that has been banned has a direct incentive to reconnect. The bypass requires only changing the address family in the multiaddr used to connect. The attack is immediately repeatable after each ban with no cooldown.

## Recommendation
Normalize the incoming `IpAddr` at the top of `is_ip_banned_until` before any lookup, mirroring the logic already present in `network_group.rs`:

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

Call `normalize_ip(ip)` at the top of `is_ip_banned_until` before `ip_to_network`. Apply the same normalization inside `ban_addr` so that bans are always stored in normalized form, preventing the inverse bypass direction as well.

## Proof of Concept
1. Node bans peer B at IPv4 `1.2.3.4`. `ban_addr` stores `IpNetwork::V4(1.2.3.4/32)` in the ban list.
2. Peer B reconnects using multiaddr `/ip6/::ffff:1.2.3.4/tcp/<port>/p2p/<peer_id>`.
3. `accept_peer` calls `peer_store.is_addr_banned`. `multiaddr_to_socketaddr` returns `SocketAddr::V6([::ffff:1.2.3.4]:port)`. `socket_addr.ip()` returns `IpAddr::V6(::ffff:1.2.3.4)`.
4. `ip_to_network` produces `IpNetwork::V6(::ffff:1.2.3.4/128)`. HashMap lookup: no entry. `iter().any` check: `IpNetwork::V4(1.2.3.4/32).contains(IpAddr::V6(::ffff:1.2.3.4))` → `false`.
5. `is_addr_banned` returns `false`. Peer B is accepted despite being banned.

Unit test to reproduce: insert a `BannedAddr` with `address: IpNetwork::V4("1.2.3.4/32".parse().unwrap())` into a `BanList`, call `is_ip_banned` with `IpAddr::V6("::ffff:1.2.3.4".parse().unwrap())`, assert the result is `true` — it currently returns `false`, confirming the bypass.

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

**File:** network/src/peer_registry.rs (L109-111)
```rust
            if peer_store.is_addr_banned(&remote_addr) {
                return Err(PeerError::Banned.into());
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

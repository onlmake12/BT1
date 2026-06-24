Audit Report

## Title
IPv4-Mapped IPv6 Address Aliasing Bypasses Peer Ban List - (File: network/src/peer_store/ban_list.rs)

## Summary
`BanList::is_ip_banned_until` does not normalize IPv4-mapped IPv6 addresses (e.g., `::ffff:1.2.3.4`) to their IPv4 equivalents before performing ban lookups. A peer banned under its IPv4 address can immediately reconnect using the IPv4-mapped IPv6 form of the same address and bypass the ban entirely. This nullifies the node's primary defense against actively misbehaving peers, allowing them to continue sending malformed messages, exhausting resources, or disrupting sync indefinitely.

## Finding Description
When a peer is banned, `ban_addr` in `peer_store_impl.rs` (L286–292) calls `ip_to_network(addr.ip())`, which maps `IpAddr::V4(1.2.3.4)` to `IpNetwork::V4(1.2.3.4/32)` and stores it in the `HashMap<IpNetwork, BannedAddr>`. [1](#0-0) 

When a new connection arrives, `accept_peer` in `peer_registry.rs` (L109) calls `peer_store.is_addr_banned(&remote_addr)`, which reaches `BanList::is_ip_banned_until` (L48–58). [2](#0-1) 

If the reconnecting peer presents `/ip6/::ffff:1.2.3.4/tcp/<port>/p2p/<peer_id>`, `multiaddr_to_socketaddr` returns `SocketAddr::V6([::ffff:1.2.3.4]:port)`, and `socket_addr.ip()` returns `IpAddr::V6(::ffff:1.2.3.4)`. `ip_to_network` then produces `IpNetwork::V6(::ffff:1.2.3.4/128)`. [3](#0-2) 

The `inner.get(&ip_network)` lookup at L50 misses because the key family is V6 while the stored entry is V4. The fallback `iter().any(|(ip_network, banned_addr)| ip_network.contains(ip))` at L56–57 evaluates `IpNetwork::V4(1.2.3.4/32).contains(IpAddr::V6(::ffff:1.2.3.4))`, which the `ipnetwork` crate returns `false` for (no cross-family containment). The function returns `false` and the peer is accepted. [4](#0-3) 

The contrast with `network_group.rs` (L31–35) is definitive: the `Group` implementation explicitly calls `ipv6.to_ipv4()` to normalize IPv4-mapped IPv6 addresses for peer diversity accounting, demonstrating developer awareness of the aliasing issue. This normalization was never applied to the ban list. `grep_search` confirms `to_ipv4` appears only in `network_group.rs` across the entire `network/src/` tree. [5](#0-4) 

## Impact Explanation
This maps to **High: Vulnerabilities or bad designs which could cause CKB network congestion with few costs**. A banned peer can trivially bypass the ban by reconnecting with the IPv4-mapped IPv6 form of its address. This allows a misbehaving peer to continuously reconnect after each ban, repeatedly sending malformed messages, triggering resource-intensive processing, or disrupting block/transaction sync. With multiple such peers, this can cause sustained network congestion or resource exhaustion on targeted nodes at negligible cost to the attacker.

## Likelihood Explanation
The attacker is an unprivileged external peer with no special access. IPv4-mapped IPv6 is supported on all major operating systems and is a standard feature of dual-stack networking. A peer that has been banned has a direct incentive to reconnect. The bypass requires only presenting the IPv4-mapped IPv6 form of the same IP in the multiaddr — no new IP address, no cryptographic material, no privileged access. The attack is immediately repeatable after every ban.

## Recommendation
In `is_ip_banned_until` (`ban_list.rs`), normalize the incoming `IpAddr` before any lookup. Add a helper:

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

Call `normalize_ip(ip)` at the top of `is_ip_banned_until` before `ip_to_network`. Apply the same normalization in `ban_addr` / `ip_to_network` or in `is_addr_banned` to ensure both the storage and lookup paths are consistent. This mirrors the logic already present in `network_group.rs` (L31–35).

## Proof of Concept
1. Start a CKB node. Connect peer B from IPv4 `1.2.3.4`. Trigger a ban (e.g., via `set_ban` RPC or score-based ban from malformed message). Confirm `ban_addr` stores `IpNetwork::V4(1.2.3.4/32)`.
2. Peer B reconnects immediately using multiaddr `/ip6/::ffff:1.2.3.4/tcp/<port>/p2p/<peer_id>`.
3. `accept_peer` calls `is_addr_banned`. `multiaddr_to_socketaddr` returns `SocketAddr::V6([::ffff:1.2.3.4]:port)`. `socket_addr.ip()` returns `IpAddr::V6(::ffff:1.2.3.4)`.
4. `ip_to_network` produces `IpNetwork::V6(::ffff:1.2.3.4/128)`. HashMap lookup misses. `iter().any(|net| net.contains(ip))` evaluates `IpNetwork::V4(1.2.3.4/32).contains(IpAddr::V6(::ffff:1.2.3.4))` → `false`.
5. `is_addr_banned` returns `false`. Peer B is accepted despite being banned.

A unit test can be written directly against `BanList`: insert a `BannedAddr` with `IpNetwork::V4(1.2.3.4/32)`, then call `is_ip_banned` with `IpAddr::V6("::ffff:1.2.3.4".parse().unwrap())` and assert it returns `true` — this test will fail on the current code, confirming the bug.

### Citations

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

**File:** network/src/peer_registry.rs (L109-111)
```rust
            if peer_store.is_addr_banned(&remote_addr) {
                return Err(PeerError::Banned.into());
            }
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

All code references check out. Let me verify one more detail about the `multiaddr_to_socketaddr` behavior in the ban check path.

Audit Report

## Title
IPv4-Mapped IPv6 Address Normalization Missing in Ban Enforcement - (File: network/src/peer_store/ban_list.rs)

## Summary
`ip_to_network()` does not normalize IPv4-mapped IPv6 addresses (`::ffff:x.x.x.x`) to their IPv4 equivalents before storing or looking up ban entries. A peer banned under its IPv4 address can immediately reconnect using the IPv4-mapped IPv6 form of the same address, bypassing the ban entirely. This is exploitable by default on dual-stack Linux hosts, which is the standard CKB deployment environment.

## Finding Description
**Root cause:** `ip_to_network()` in `network/src/peer_store/types.rs` (L152–156) maps `IpAddr::V4` → `IpNetwork::V4(.../32)` and `IpAddr::V6` → `IpNetwork::V6(.../128)` with no normalization:

```rust
pub fn ip_to_network(ip: IpAddr) -> IpNetwork {
    match ip {
        IpAddr::V4(ipv4) => IpNetwork::V4(ipv4.into()),
        IpAddr::V6(ipv6) => IpNetwork::V6(ipv6.into()),
    }
}
```

**Ban storage path:** `ban_addr()` in `peer_store_impl.rs` (L286–292) calls `multiaddr_to_socketaddr(addr)` to extract the IP, then `ip_to_network(addr.ip())`. When a peer connects via IPv4 (`1.2.3.4`), this stores `IpNetwork::V4(1.2.3.4/32)` in the `HashMap<IpNetwork, BannedAddr>`.

**Ban check path:** `is_ip_banned_until()` in `ban_list.rs` (L48–58) performs two checks:
1. Exact `HashMap` lookup: `IpNetwork::V6(::ffff:1.2.3.4/128)` ≠ `IpNetwork::V4(1.2.3.4/32)` → miss.
2. Fallback `ip_network.contains(ip)` loop: `IpNetwork::V4(1.2.3.4/32).contains(IpAddr::V6(::ffff:1.2.3.4))` → always `false` (the `ipnetwork` crate never cross-matches a V4 network against a V6 address).

**Inbound connection gate:** `accept_peer()` in `peer_registry.rs` (L109–110) calls `peer_store.is_addr_banned(&remote_addr)` for every inbound connection. When the reconnecting peer arrives via a dual-stack socket as `::ffff:1.2.3.4`, the ban check returns `false` and the peer is accepted.

**Production confirmation:** The RPC documentation in `rpc/src/module/net.rs` (L107–115) shows a real production peer simultaneously listed as `/ip6/::ffff:18.185.102.19/tcp/8115/...` and `/ip4/18.185.102.19/tcp/8115/...`, confirming the p2p library does not normalize IPv4-mapped IPv6 addresses when constructing multiaddrs from incoming connections.

**Secondary impact:** `base_addr()` in `peer_store/mod.rs` (L92–104) only strips `Ws`, `Wss`, `Memory`, `Tls` protocols without normalizing IPv4-mapped IPv6. `AddrManager`'s `addr_to_id: HashMap<Multiaddr, u64>` (L15) stores both forms as distinct entries. The per-IP deduplication in `fetch_random` (L59–72) also fails since `IpAddr::V4(1.2.3.4)` and `IpAddr::V6(::ffff:1.2.3.4)` are distinct `HashSet` keys.

## Impact Explanation
**High: Vulnerabilities or bad designs which could cause CKB network congestion with few costs.**

The ban mechanism is completely ineffective on dual-stack nodes (the default Linux deployment). An attacker banned for misbehavior (e.g., sending invalid blocks/headers, score-based banning via `report()` when score drops below `ban_score = 40`) can immediately reconnect using the IPv4-mapped IPv6 form of the same address. The attacker can cycle through ban-reconnect indefinitely, continuously consuming inbound connection slots (capped by `max_inbound`), exhausting peer store capacity (`ADDR_COUNT_LIMIT = 16384`), and forcing repeated processing of invalid protocol messages — degrading the node's ability to maintain connections with legitimate peers and contributing to network-level congestion.

## Likelihood Explanation
Modern Linux servers bind to `::` (IPv6 wildcard) by default with `IPV6_V6ONLY = 0`, meaning IPv4 connections arrive as IPv4-mapped IPv6 addresses at the socket layer. The p2p library propagates this representation into the multiaddr without normalization, as confirmed by the production RPC example. No special attacker capability is required beyond reconnecting — the OS handles the address mapping transparently. The attack is repeatable with zero cost per reconnection attempt and requires no victim mistakes.

## Recommendation
Normalize IPv4-mapped IPv6 addresses to their IPv4 equivalents in `ip_to_network()` in `network/src/peer_store/types.rs`:

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

Apply the same normalization in `base_addr()` in `network/src/peer_store/mod.rs` so that `AddrManager` treats `/ip4/x.x.x.x/...` and `/ip6/::ffff:x.x.x.x/...` as the same key, fixing both the ban bypass and the peer store slot inflation.

## Proof of Concept
1. Run a CKB node on a dual-stack Linux host (default configuration, listening on `::`).
2. Connect a peer from `1.2.3.4` via IPv4; trigger score-based banning (send invalid headers until score drops below `ban_score = 40`). Verify `ban_addr` stores `IpNetwork::V4(1.2.3.4/32)` via the ban list RPC.
3. Immediately reconnect from the same IP. On a dual-stack socket, the OS presents the connection as `::ffff:1.2.3.4`. The p2p library constructs multiaddr `/ip6/::ffff:1.2.3.4/tcp/.../p2p/<id>`.
4. `accept_peer()` calls `is_addr_banned()` → `is_ip_banned_until(IpAddr::V6(::ffff:1.2.3.4))` → `ip_to_network` produces `IpNetwork::V6(::ffff:1.2.3.4/128)` → HashMap miss → `contains()` loop returns `false` → peer accepted.
5. Confirm via RPC `get_peers` that the previously-banned peer is now connected under its IPv6-mapped address. Repeat indefinitely to exhaust inbound slots.
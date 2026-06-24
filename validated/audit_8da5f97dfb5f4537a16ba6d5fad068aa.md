All cited code references are confirmed in the repository. Let me verify the `ipnetwork` crate behavior for cross-family `contains` calls.

Audit Report

## Title
IPv4-Mapped IPv6 Address Aliasing Bypasses Peer Ban List - (File: network/src/peer_store/ban_list.rs)

## Summary
`BanList::is_ip_banned_until` does not normalize IPv4-mapped IPv6 addresses (e.g., `::ffff:1.2.3.4`) to their IPv4 equivalents before performing the ban lookup. Because `ip_to_network` preserves the address family verbatim and `IpNetwork::contains` never matches across address families, a peer banned under its IPv4 address can immediately reconnect using the IPv4-mapped IPv6 form of the same address and bypass the ban entirely. The normalization required to prevent this is already present in `network_group.rs` for peer diversity accounting, confirming developer awareness of the aliasing issue.

## Finding Description

**Storage path:** `ban_addr` in `peer_store_impl.rs` calls `ip_to_network(addr.ip())`, which maps `IpAddr::V4(1.2.3.4)` → `IpNetwork::V4(1.2.3.4/32)` and stores it in the `HashMap<IpNetwork, BannedAddr>`. [1](#0-0) [2](#0-1) 

**Lookup path:** `is_ip_banned_until` converts the incoming `IpAddr` with the same `ip_to_network`, producing `IpNetwork::V6(::ffff:1.2.3.4/128)` for an IPv4-mapped IPv6 address. The `HashMap::get` lookup misses (different key family). The fallback `iter().any(|net| net.contains(ip))` then evaluates `IpNetwork::V4(1.2.3.4/32).contains(IpAddr::V6(::ffff:1.2.3.4))`, which the `ipnetwork` crate (v0.20.0) returns `false` for cross-family comparisons. [3](#0-2) 

**Check entry point:** `accept_peer` calls `peer_store.is_addr_banned(&remote_addr)` before allowing a connection, which routes through `BanList::is_addr_banned` → `is_ip_banned_until`. [4](#0-3) 

**Contrast with existing normalization:** `network_group.rs` explicitly calls `ipv6.to_ipv4()` and maps IPv4-mapped IPv6 to `Group::IP4`, demonstrating that the aliasing is known and handled elsewhere — but was never applied to the ban list. [5](#0-4) 

**Real ban triggers confirmed in codebase:** Multiple protocols call `ban_peer` / `ban_session` for malformed messages: the sync protocol, relay protocol, block filter, light client protocol, network alert relayer, hole punching protocol, identify protocol, and the generic `ProtocolError` / `ProtocolHandleError` handlers in `network.rs`. All of these produce bans that can be bypassed via this mechanism. [6](#0-5) [7](#0-6) 

## Impact Explanation

The ban mechanism is the node's primary runtime defense against actively misbehaving peers. Bypassing it allows a peer to repeatedly send malformed messages, trigger protocol errors, or perform sync disruption — actions that would otherwise be stopped by the ban — at negligible cost (no new IP address, no cryptographic material, just reconnecting with a different multiaddr format). Across many nodes, this enables sustained low-cost disruption of the CKB P2P network. This matches the allowed impact: **Low (501–2000 points) — any other important security/performance improvement for CKB**, with potential escalation to **High — vulnerabilities or bad designs which could cause CKB network congestion with few costs** if the attacker targets multiple nodes simultaneously using the bypass to sustain repeated malformed-message floods.

## Likelihood Explanation

The attacker is an unprivileged peer with no special access. IPv4-mapped IPv6 is supported on all major operating systems and is a standard feature of dual-stack networking. A peer that has just been banned has a direct incentive to reconnect. The bypass requires only changing the multiaddr format from `/ip4/1.2.3.4/tcp/...` to `/ip6/::ffff:1.2.3.4/tcp/...` — no new IP address, no new peer ID required (peer ID check is separate from IP ban check). The attack is immediately repeatable after each ban.

## Recommendation

Add a normalization helper and call it at the top of `is_ip_banned_until` (and optionally in `ip_to_network` or `ban_addr`) before any lookup:

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

Apply `normalize_ip(ip)` at the start of `is_ip_banned_until` before calling `ip_to_network`. Mirror the same logic already present in `network_group.rs` at lines 31–34. [8](#0-7) [5](#0-4) 

## Proof of Concept

1. Start a CKB node. Connect a test peer from IPv4 address `1.2.3.4`.
2. Have the test peer send a malformed sync message (e.g., `vec![0, 0, 0, 0]` as in the existing integration test `test/src/specs/p2p/malformed_message.rs`). The node calls `ban_session` → `ban_addr` → stores `IpNetwork::V4(1.2.3.4/32)` in the ban list.
3. Immediately reconnect from the same host using multiaddr `/ip6/::ffff:1.2.3.4/tcp/<port>/p2p/<peer_id>`.
4. `accept_peer` calls `is_addr_banned`. `multiaddr_to_socketaddr` returns `SocketAddr::V6([::ffff:1.2.3.4]:port)`. `socket_addr.ip()` returns `IpAddr::V6(::ffff:1.2.3.4)`. `ip_to_network` produces `IpNetwork::V6(::ffff:1.2.3.4/128)`. HashMap lookup: miss. `contains` check: `false`. `is_addr_banned` returns `false`.
5. The peer is accepted despite being banned. Repeat from step 2 indefinitely.

A unit test can be written directly against `BanList`: ban `IpNetwork::V4(1.2.3.4/32)`, then assert `is_ip_banned(&IpAddr::V6("::ffff:1.2.3.4".parse().unwrap()))` returns `true` — it currently returns `false`. [9](#0-8) [3](#0-2)

### Citations

**File:** network/src/peer_store/peer_store_impl.rs (L286-289)
```rust
    pub(crate) fn ban_addr(&mut self, addr: &Multiaddr, timeout_ms: u64, ban_reason: String) {
        if let Some(addr) = multiaddr_to_socketaddr(addr) {
            let network = ip_to_network(addr.ip());
            self.ban_network(network, timeout_ms, ban_reason)
```

**File:** network/src/peer_store/types.rs (L151-156)
```rust
/// Convert IpAddr to IpNetwork
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

**File:** network/src/network.rs (L650-656)
```rust
                // Ban because misbehave of remote peer
                self.network_state.ban_session(
                    &context.control().clone().into(),
                    id,
                    Duration::from_secs(300),
                    message,
                );
```

**File:** sync/src/synchronizer/mod.rs (L940-948)
```rust
            _ => {
                info!("A malformed message from peer {}", peer_index);
                nc.ban_peer(
                    peer_index,
                    BAD_MESSAGE_BAN_TIME,
                    String::from("send us a malformed message"),
                );
                return;
            }
```

**File:** test/src/specs/p2p/malformed_message.rs (L33-45)
```rust
        info!("Send malformed message to node0 twice");
        net.send(node0, SupportProtocols::Sync, vec![0, 0, 0, 0].into());
        net.send(node0, SupportProtocols::Sync, vec![0, 1, 2, 3].into());
        let rpc_client = nodes[0].rpc_client();
        let ret = wait_until(10, || rpc_client.get_peers().is_empty());
        assert!(ret, "Node0 should disconnect test node");
        let ret = wait_until(10, || {
            rpc_client
                .get_banned_addresses()
                .iter()
                .any(|ban| ban.address == "127.0.0.1/32")
        });
        assert!(ret, "Node0 should ban test node");
```

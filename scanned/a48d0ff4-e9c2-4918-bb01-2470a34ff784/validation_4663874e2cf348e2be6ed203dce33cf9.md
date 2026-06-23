Looking at the exact code paths involved:

The code evidence is clear and the path is concrete. Here is the analysis:

---

**`Group::None` bucket collapse in `try_evict_inbound_peer` — (`network/src/network_group.rs`, `network/src/peer_registry.rs`)**

### Summary

`Group::from(&Multiaddr)` returns `Group::None` for every address that `multiaddr_to_socketaddr` cannot resolve to an IP (onion3, DNS-only, etc.). The eviction algorithm in `try_evict_inbound_peer` selects the **largest** network group and randomly evicts one peer from it. An attacker who floods inbound slots from onion/non-IP addresses inflates `Group::None` into the dominant group, causing the eviction algorithm to preferentially target that bucket — which also contains all legitimate privacy-transport peers.

---

### Finding Description

In `network/src/network_group.rs`, the fallback path: [1](#0-0) 

returns `Group::None` for any address that is not a resolvable IPv4/IPv6 socket address. This includes onion3 and DNS-only multiaddrs.

In `network/src/peer_registry.rs`, `try_evict_inbound_peer` groups surviving candidates by `network_group()` and selects the largest group as the eviction target: [2](#0-1) 

A random peer is then evicted from that group: [3](#0-2) 

Because all non-IP peers share a single `Group::None` key, the diversity protection that grouping is meant to provide is entirely absent for privacy-transport peers. An attacker who opens N onion-addressed inbound sessions inflates `Group::None` to be the largest group, making it the permanent eviction target.

---

### Impact Explanation

The protection rounds (lowest ping, most recent message, longest connection time) each protect only `EVICTION_PROTECT_PEERS = 8` peers: [4](#0-3) 

A fresh attacker connection has no ping data and no recent messages, so it is not protected — but neither are fresh legitimate onion peers. After protections, if the attacker holds more `Group::None` slots than legitimate onion peers, the eviction loop continuously targets `Group::None`. The attacker reconnects immediately after being evicted, maintaining slot dominance. Legitimate onion peers, evicted at a rate proportional to their minority share of `Group::None`, cannot maintain stable connections.

This degrades inbound connectivity for all privacy-transport peers and, at scale, can contribute to an eclipse condition for nodes that rely on onion inbound connections for peer diversity.

---

### Likelihood Explanation

- Requires only the ability to open inbound TCP/onion sessions — no authentication, no PoW, no privileged access.
- The node's default configuration accepts inbound connections.
- The attacker needs enough simultaneous connections to outnumber legitimate `Group::None` peers, which is feasible given typical inbound limits.
- The protection rounds provide partial mitigation for long-lived legitimate peers but offer no protection for newly connecting honest peers.

---

### Recommendation

Assign a distinct, per-peer-identity group to `Group::None` addresses (e.g., hash the full multiaddr or peer ID into a synthetic group key) so that each non-IP peer occupies its own bucket rather than sharing one. Alternatively, cap the number of inbound connections accepted per `Group::None` bucket, mirroring the per-/16 or per-/32 limits applied to IPv4/IPv6 groups.

---

### Proof of Concept

1. Start a CKB node with `max_inbound = 125`.
2. Connect 100 inbound sessions from onion3 addresses (attacker-controlled) — all map to `Group::None`.
3. Connect 10 inbound sessions from legitimate onion peers — also `Group::None`.
4. Trigger eviction by connecting one more inbound peer.
5. Observe: `try_evict_inbound_peer` selects `Group::None` (size 110) as the largest group and randomly evicts one peer. The attacker immediately reconnects.
6. Repeat: legitimate onion peers are evicted at ~10/110 ≈ 9% rate per eviction event while the attacker maintains ~91% of `Group::None` slots, systematically displacing honest privacy-transport peers. [5](#0-4) [6](#0-5)

### Citations

**File:** network/src/network_group.rs (L12-42)
```rust
impl From<&Multiaddr> for Group {
    fn from(multiaddr: &Multiaddr) -> Group {
        if let Some(socket_addr) = multiaddr_to_socketaddr(multiaddr) {
            let ip_addr = socket_addr.ip();
            if ip_addr.is_loopback() {
                return Group::LocalNetwork;
            }
            // TODO uncomment after ip feature stable
            // if !ip_addr.is_global() {
            //     // Global NetworkGroup
            //     return Group::GlobalNetwork
            // }

            // IPv4 NetworkGroup
            if let IpAddr::V4(ipv4) = ip_addr {
                let bits = ipv4.octets();
                return Group::IP4([bits[0], bits[1]]);
            }
            // IPv6 NetworkGroup
            if let IpAddr::V6(ipv6) = ip_addr {
                if let Some(ipv4) = ipv6.to_ipv4() {
                    let bits = ipv4.octets();
                    return Group::IP4([bits[0], bits[1]]);
                }
                let bits = ipv6.octets();
                return Group::IP6([bits[0], bits[1], bits[2], bits[3]]);
            }
        }
        // Can't group addr
        Group::None
    }
```

**File:** network/src/peer_registry.rs (L17-17)
```rust
pub(crate) const EVICTION_PROTECT_PEERS: usize = 8;
```

**File:** network/src/peer_registry.rs (L142-211)
```rust
    fn try_evict_inbound_peer(&self, _peer_store: &PeerStore) -> Option<SessionId> {
        let mut candidate_peers = {
            self.peers
                .values()
                .filter(|peer| peer.is_inbound() && !peer.is_whitelist)
                .collect::<Vec<_>>()
        };
        // Protect peers based on characteristics that an attacker hard to simulate or manipulate
        // Protect peers which has the lowest ping
        sort_then_drop(
            &mut candidate_peers,
            EVICTION_PROTECT_PEERS,
            |peer1, peer2| {
                let peer1_ping = peer1
                    .ping_rtt
                    .map(|p| p.as_secs())
                    .unwrap_or_else(|| u64::MAX);
                let peer2_ping = peer2
                    .ping_rtt
                    .map(|p| p.as_secs())
                    .unwrap_or_else(|| u64::MAX);
                peer2_ping.cmp(&peer1_ping)
            },
        );

        // Protect peers which most recently sent messages
        sort_then_drop(
            &mut candidate_peers,
            EVICTION_PROTECT_PEERS,
            |peer1, peer2| {
                let now = Instant::now();
                let peer1_last_message = peer1
                    .last_ping_protocol_message_received_at
                    .map(|t| now.saturating_duration_since(t).as_secs())
                    .unwrap_or_else(|| u64::MAX);
                let peer2_last_message = peer2
                    .last_ping_protocol_message_received_at
                    .map(|t| now.saturating_duration_since(t).as_secs())
                    .unwrap_or_else(|| u64::MAX);
                peer2_last_message.cmp(&peer1_last_message)
            },
        );
        // Protect half peers which have the longest connection time
        let protect_peers = candidate_peers.len() >> 1;
        sort_then_drop(&mut candidate_peers, protect_peers, |peer1, peer2| {
            peer2.connected_time.cmp(&peer1.connected_time)
        });

        // Group peers by network group
        let evict_group = candidate_peers
            .into_iter()
            .fold(
                HashMap::new(),
                |mut groups: HashMap<Group, Vec<&Peer>>, peer| {
                    groups.entry(peer.network_group()).or_default().push(peer);
                    groups
                },
            )
            .values()
            .max_by_key(|group| group.len())
            .cloned()
            .unwrap_or_default();

        // randomly evict a peer
        let mut rng = thread_rng();
        evict_group.choose(&mut rng).map(|peer| {
            debug!("Disconnect inbound peer {:?}", peer.connected_addr);
            peer.session_id
        })
    }
```

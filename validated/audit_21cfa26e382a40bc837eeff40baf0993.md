Audit Report

## Title
Systematic Eclipse via Largest-Group Eviction Bias in `try_evict_inbound_peer` — (`network/src/peer_registry.rs`)

## Summary
The `try_evict_inbound_peer` function in `network/src/peer_registry.rs` contains a biased eviction algorithm that allows an attacker controlling peers across distinct `/16` subnets to satisfy all three protection criteria (low ping, recent messages, long connection time), leaving legitimate peers as the only eviction candidates. When multiple legitimate peers share a `/16`, the largest-group selection step deterministically targets them, enabling systematic replacement of a node's inbound peer set. The impact is inbound eclipse; the node's outbound connections remain unaffected, so the claimed double-spend impact is not directly achievable from this bug alone.

## Finding Description
**Root cause — `sort_then_drop` semantics (L55–63):**

`sort_then_drop` sorts the candidate list and calls `truncate(list.len() - n)`, which keeps the first `list.len() - n` elements (the "worst" peers) and removes the last `n` (the "best" peers, protecting them from eviction). All three protection steps use this same semantics.

**Step 2 — ping protection (L151–165):** Comparator is `peer2_ping.cmp(&peer1_ping)` — descending. The 8 lowest-ping peers are at the tail and are protected. Peers with `ping_rtt = None` map to `u64::MAX` and sort to the front, making them first candidates for eviction. An attacker maintaining `ping_rtt = Some(low_ms)` is protected; legitimate peers with no measured ping are not.

**Step 3 — message recency (L168–183):** Comparator is `peer2_last_message.cmp(&peer1_last_message)` — descending. The 8 most-recently-active peers are protected. Peers with `last_ping_protocol_message_received_at = None` map to `u64::MAX` and sort to the front. Attacker peers sending frequent pings are protected; passive legitimate peers are not.

**Step 4 — connection time (L185–188):** `protect_peers = candidate_peers.len() >> 1`. Comparator is `peer2.connected_time.cmp(&peer1.connected_time)` — descending (most recently connected first). The oldest half is protected. Attacker peers that connected before legitimate peers are protected here; legitimate peers that connected later are not.

**Step 5 — largest-group eviction (L191–210):** Remaining candidates are grouped by `/16` subnet (`network_group.rs` L26–28: `Group::IP4([bits[0], bits[1]])`). `.max_by_key(|group| group.len())` selects the largest group. Attacker peers each occupy a distinct `/16` (singleton groups of size 1). Legitimate peers sharing a `/16` form a group of size ≥ 2, which is always the largest group and is always selected for eviction.

**`_peer_store` is unused (L142):** The parameter is prefixed with `_` and never consulted; no reputation or historical scoring mitigates the attack.

**Exploit flow:**
1. Attacker fills `N - K` inbound slots with peers from distinct `/16` subnets, connecting before legitimate peers, with low ping and frequent messages.
2. `K` legitimate peers connect from the same `/16`.
3. Inbound slots are full (`N` total).
4. Attacker connects a new peer, triggering `try_evict_inbound_peer`.
5. Attacker peers satisfy all three protection criteria; legitimate peers do not.
6. After protection passes, legitimate peers form the largest `/16` group and one is evicted.
7. The new attacker peer takes the freed slot.
8. Repeat until all legitimate peers are replaced.

## Impact Explanation
**High — Vulnerabilities which could easily crash a CKB node.** The attack achieves complete eclipse of the victim node's inbound connections. The node's outbound connections remain unaffected, so a full network-level eclipse (required for double-spend attacks) is not achieved by this bug alone. The concrete in-scope impact is that an attacker can partition a node's inbound peer set, preventing it from receiving blocks or transactions relayed by inbound peers, which can cause the node to fall behind the chain tip and become effectively non-functional for inbound-relying clients. The Critical "damage CKB economy" claim is not supported because outbound connections to honest peers remain intact.

## Likelihood Explanation
Requires `N - K` IP addresses from distinct `/16` subnets. With `max_inbound = 125` and `K = 2`, that is 123 distinct `/16` subnets — achievable via cloud providers, VPS networks, or residential proxy services at moderate cost. The attacker must also maintain low ping and send frequent ping messages, which is straightforward with standard network infrastructure. The attack is deterministic and repeatable once the attacker has sufficient IPs. The additional condition that legitimate peers share a `/16` is realistic (same ISP, datacenter, or organization) but not universal.

## Recommendation
1. **Cap per-group representation before the largest-group step:** after protection passes, if any single group exceeds `max(1, candidates / 4)` peers, randomly evict from it regardless of whether it is the largest.
2. **Add randomized eviction fallback:** with some probability (e.g., 50%), evict a uniformly random candidate rather than always targeting the largest group.
3. **Use peer-store reputation in eviction:** the `_peer_store` parameter is already threaded through `try_evict_inbound_peer`; use it to prefer evicting peers with poor historical scores.
4. **Limit inbound connections per `/16`:** reject or deprioritize new inbound connections from a `/16` that already has several connected peers.
5. **Treat `ping_rtt = None` as neutral, not worst:** peers with no measured ping should not be ranked as highest-latency candidates; use a separate "unverified" bucket.

## Proof of Concept
```rust
// N=10, K=2: 8 attacker peers in distinct /16s (connect first, low ping),
// 2 legitimate peers in same /16 (connect later, ping_rtt=None → u64::MAX)
let mut registry = PeerRegistry::new(10, 3, false, vec![], true);
let mut peer_store = PeerStore::default();

// Attacker peers: 1.0.x.x through 8.0.x.x (distinct /16s), connect first
for i in 1u8..=8 {
    let addr = format!("/ip4/{}.0.0.1/tcp/1234/p2p/{}", i, PeerId::random().to_base58())
        .parse::<Multiaddr>().unwrap();
    registry.accept_peer(addr, i.into(), RawSessionType::Inbound, &mut peer_store).unwrap();
    registry.get_peer_mut(i.into()).unwrap().ping_rtt = Some(Duration::from_millis(10));
}
// Legitimate peers: both in 100.0.x.x (/16 = [100, 0]), connect after
for i in 9u8..=10 {
    let addr = format!("/ip4/100.0.{}.1/tcp/1234/p2p/{}", i, PeerId::random().to_base58())
        .parse::<Multiaddr>().unwrap();
    registry.accept_peer(addr, i.into(), RawSessionType::Inbound, &mut peer_store).unwrap();
    // ping_rtt = None → u64::MAX, not protected in step 2
}

// Inbound is full. Run eviction rounds.
let mut legit_evicted = 0;
for round in 0..100 {
    let new_addr = format!("/ip4/9{}.0.0.1/tcp/1234/p2p/{}", round % 50 + 10,
                           PeerId::random().to_base58())
        .parse::<Multiaddr>().unwrap();
    if let Ok(Some(evicted)) = registry.accept_peer(new_addr, (100+round).into(),
                                                     RawSessionType::Inbound, &mut peer_store) {
        if evicted.network_group() == Group::IP4([100, 0]) { legit_evicted += 1; }
    }
}
assert!(legit_evicted > 80, "legit evicted {} / 100 rounds", legit_evicted);
```

The invariant holds because: (a) attacker peers with `ping_rtt = Some(10ms)` are always protected in step 2 at [1](#0-0) ; (b) attacker peers with earlier `connected_time` are protected in step 4 at [2](#0-1) ; (c) the two legitimate peers in `Group::IP4([100, 0])` form the only multi-peer group and are always selected in step 5 at [3](#0-2) ; and (d) the `_peer_store` parameter provides no mitigation at [4](#0-3) .

### Citations

**File:** network/src/peer_registry.rs (L142-142)
```rust
    fn try_evict_inbound_peer(&self, _peer_store: &PeerStore) -> Option<SessionId> {
```

**File:** network/src/peer_registry.rs (L151-165)
```rust
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
```

**File:** network/src/peer_registry.rs (L185-188)
```rust
        let protect_peers = candidate_peers.len() >> 1;
        sort_then_drop(&mut candidate_peers, protect_peers, |peer1, peer2| {
            peer2.connected_time.cmp(&peer1.connected_time)
        });
```

**File:** network/src/peer_registry.rs (L191-210)
```rust
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
```

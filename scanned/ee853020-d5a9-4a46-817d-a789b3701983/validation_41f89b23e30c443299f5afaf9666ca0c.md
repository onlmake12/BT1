Audit Report

## Title
Inbound Peer Eviction Protection Tiers Gameable via Sub-Second Ping RTT Ties and Unsolicited Ping Spam — (`network/src/peer_registry.rs`)

## Summary
`try_evict_inbound_peer` in `network/src/peer_registry.rs` uses coarse `as_secs()` granularity for both the ping-RTT protection tier and the most-recent-message protection tier. An attacker with 16 inbound connections (8 responding to pings within <1 second, 8 spamming unsolicited pings) can simultaneously capture all 8 slots in each tier, leaving any honest peer as the sole eviction candidate and guaranteeing its removal on every subsequent inbound connection attempt.

## Finding Description

**`sort_then_drop` mechanics** (`peer_registry.rs` L55–63): After sorting by the comparator, `list.truncate(list.len() - n)` removes the last `n` elements. Those last `n` elements are the "protected" peers removed from the eviction candidate pool. The first `list.len() - n` elements remain as eviction candidates.

**Tier 1 — Ping RTT** (`peer_registry.rs` L151–165): The comparator sorts descending by `peer.ping_rtt.map(|p| p.as_secs()).unwrap_or(u64::MAX)`. Any peer with RTT < 1 second maps to `as_secs() == 0`. After descending sort, all such peers cluster at the tail and are protected. Because `as_secs()` truncates sub-millisecond precision, 8 attacker peers with RTT of e.g. 500 ms are indistinguishable from each other and trivially fill all 8 protection slots.

**Tier 2 — Most-recent ping message** (`peer_registry.rs` L168–183 and `ping.rs` L62–68): `ping_received` sets `peer.last_ping_protocol_message_received_at = Some(Instant::now())` every time an *incoming* ping is received from the remote peer, with no rate limit. An attacker can spam pings to keep their timestamp at `now`, yielding `now.saturating_duration_since(t).as_secs() == 0`. After descending sort by time-since-message, 8 such peers fill all 8 Tier 2 protection slots.

**Tier 3 — Longest connection time** (`peer_registry.rs` L185–188): `protect_peers = candidate_peers.len() >> 1`. With only 1 honest peer remaining after Tiers 1 and 2, `1 >> 1 = 0`, so no peer is protected here.

**Full attack trace (8A + 8B + 1 honest = 17 inbound peers, max_inbound = 16):**

| Step | Candidates remaining |
|---|---|
| Initial | 8A (RTT=0) + 8B (recent ping) + 1 honest = 17 |
| Tier 1: protect 8 lowest RTT | 8B (u64::MAX RTT) + 1 honest = 9 |
| Tier 2: protect 8 most-recent-message | 1 honest |
| Tier 3: protect `1>>1=0` | 1 honest |
| Network group + random evict | honest peer evicted (p=1.0) |

Existing guards are insufficient: there is no per-IP inbound connection cap in the eviction path (`accept_peer` only checks `connection_status.non_whitelist_inbound >= self.max_inbound`), and `ping_received` applies no rate limiting.

## Impact Explanation

An attacker maintaining 16 inbound connections guarantees that every subsequent inbound connection attempt evicts an honest peer. Over time the attacker monopolizes all inbound slots, degrading the victim node's peer diversity and enabling targeted censorship of transactions and blocks on inbound relay paths. This constitutes a partial eclipse attack on inbound connections, mapping to **High** severity: a bad design that can cause CKB network degradation (reduced peer diversity, inbound relay censorship) with minimal cost to the attacker.

## Likelihood Explanation

- Establishing 16 inbound connections from distinct IPs is straightforward; no per-IP cap exists in the eviction path.
- Responding to pings within <1 second is trivially achievable on any modern network.
- Sending unsolicited ping messages is a standard P2P protocol operation; `ping_received` imposes no rate limit and is reachable by any connected peer.
- No privileged access, leaked keys, or majority hashpower is required.
- The attack is repeatable: each new inbound connection attempt evicts an honest peer with probability 1.0 as long as the 16 attacker connections are maintained.

## Recommendation

1. **Use sub-second RTT granularity**: Replace `p.as_secs()` with `p.as_millis()` or `p.as_micros()` in the Tier 1 comparator (`peer_registry.rs` L157) so that sub-second attacker peers cannot all tie at 0 and crowd out genuinely low-latency honest peers.

2. **Separate ping-send and ping-receive timestamps**: `last_ping_protocol_message_received_at` should only be updated on *pong* receipt (i.e., in `pong_received`, `ping.rs` L71–79), not on receipt of an unsolicited incoming ping (`ping_received`, `ping.rs` L62–68). This prevents an attacker from refreshing the Tier 2 timestamp by spamming pings.

3. **Add per-IP or per-/16-subnet inbound connection limits** upstream of the eviction path to raise the cost of establishing 16 simultaneous inbound connections.

4. **Add a network-group diversity check** to the protection tiers so that a single network group cannot fill an entire protection tier.

## Proof of Concept

```rust
// Minimal reproducible test
let mut peer_store = PeerStore::default();
// max_inbound = 16, so 17th connection triggers eviction
let mut registry = PeerRegistry::new(16, 3, false, vec![], true);

// Attacker group A: 8 peers, ping_rtt = 500ms → as_secs() == 0 (Tier 1 protected)
for i in 0..8u32 {
    registry.accept_peer(attacker_addr_a(i), session_a(i), RawSessionType::Inbound, &mut peer_store).unwrap();
    registry.get_peer_mut(session_a(i)).unwrap().ping_rtt = Some(Duration::from_millis(500));
}

// Attacker group B: 8 peers, last_ping_protocol_message_received_at = now (Tier 2 protected)
for i in 0..8u32 {
    registry.accept_peer(attacker_addr_b(i), session_b(i), RawSessionType::Inbound, &mut peer_store).unwrap();
    registry.get_peer_mut(session_b(i)).unwrap()
        .last_ping_protocol_message_received_at = Some(Instant::now());
}

// 1 honest peer: no ping data, no recent messages
registry.accept_peer(honest_addr(), session_honest(), RawSessionType::Inbound, &mut peer_store).unwrap();

// 17th connection triggers eviction — honest peer is always the sole candidate
let result = registry.accept_peer(new_addr(), 100.into(), RawSessionType::Inbound, &mut peer_store).unwrap();
let evicted = result.unwrap(); // must be Some(honest_peer)
assert_eq!(evicted.session_id, session_honest());
```

After Tier 1 removes group A (RTT=0) and Tier 2 removes group B (time-since=0), only the honest peer remains as a candidate. Tier 3 protects `1>>1=0` peers. The honest peer is evicted with probability 1.0 on every invocation.
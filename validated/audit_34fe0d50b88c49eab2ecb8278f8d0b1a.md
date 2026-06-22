### Title
Inbound Peer Eviction Protection Tiers Simultaneously Gameable via Ping Spam and Fast Pong — (`network/src/peer_registry.rs`)

---

### Summary

The `try_evict_inbound_peer` function in `network/src/peer_registry.rs` implements two sequential protection tiers — lowest-ping-RTT and most-recent-ping-message — both of which can be simultaneously and independently captured by attacker-controlled inbound peers. With 16 attacker connections (8 per tier), all honest peers are left as the sole eviction candidates, guaranteeing their removal on every new inbound connection attempt.

---

### Finding Description

`try_evict_inbound_peer` runs three sequential `sort_then_drop` passes over inbound non-whitelist peers:

**`sort_then_drop` mechanics** — it sorts the list by the comparator, then calls `list.truncate(list.len() - n)`, which *removes* the last `n` elements. Those last `n` elements are the "protected" peers dropped from the eviction candidate pool. [1](#0-0) 

**Tier 1 — Ping RTT (protects 8 peers):**
The comparator is `peer2_ping.cmp(&peer1_ping)` (descending). Peers with no ping data get `u64::MAX`; peers with sub-second RTT get `ping_rtt.as_secs() == 0`. After descending sort, the 8 lowest-RTT peers are at the tail and are dropped (protected). Any attacker peer that responds to the victim's ping within <1 second achieves `as_secs() == 0` and occupies a protection slot. [2](#0-1) 

The coarse `as_secs()` truncation is the root cause: every sub-second peer is indistinguishable from every other, so 8 attacker peers trivially fill all 8 slots. [3](#0-2) 

**Tier 2 — Most-recent ping message (protects 8 peers):**
`last_ping_protocol_message_received_at` is set to `Instant::now()` every time an *incoming* ping is received from the remote peer. [4](#0-3) 

An attacker can spam ping messages to the victim at will, keeping their `last_ping_protocol_message_received_at` at `now` (time-since = 0 seconds). Honest peers with no ping activity get `u64::MAX`. After descending sort by time-since-message, the 8 most-recent peers are at the tail and protected. [5](#0-4) 

**Tier 3 — Longest connection time (protects half of remaining):**
With only 1 honest peer remaining after Tiers 1 and 2, `protect_peers = 1 >> 1 = 0`, so no one is protected here. [6](#0-5) 

**Full attack trace with 17 peers (8A + 8B + 1 honest):**

| After step | Candidates remaining |
|---|---|
| Initial | 8A + 8B + 1 honest = 17 |
| Tier 1 (protect 8A, RTT=0) | 8B + 1 honest = 9 |
| Tier 2 (protect 8B, recent msg) | 1 honest |
| Tier 3 (protect `1>>1=0`) | 1 honest |
| Network group + random evict | honest peer evicted |

The honest peer is evicted with probability 1.0 on every new inbound connection attempt.

---

### Impact Explanation

An attacker who maintains 16 inbound connections to the victim node (8 responding quickly to pings, 8 spamming pings) guarantees that every subsequent inbound connection attempt evicts an honest peer rather than an attacker peer. Over time, the attacker monopolizes all inbound slots. This degrades the victim's peer diversity, enables targeted transaction/block censorship on inbound relay paths, and contributes to a partial eclipse attack on inbound connections.

---

### Likelihood Explanation

- Establishing 16 inbound connections from distinct IPs is straightforward; there is no per-IP inbound connection cap visible in the eviction path. [7](#0-6) 
- Responding to pings within <1 second is trivially achievable on any modern network.
- Sending unsolicited ping messages is a standard P2P protocol operation; `ping_received` imposes no rate limit. [4](#0-3) 
- No privileged access, leaked keys, or majority hashpower is required.

---

### Recommendation

1. **Use sub-second RTT granularity**: Replace `p.as_secs()` with `p.as_millis()` or `p.as_micros()` in the ping-RTT comparator so that fast-responding attacker peers cannot all tie at 0 and crowd out genuinely low-latency honest peers. [3](#0-2) 

2. **Separate ping-send and ping-receive timestamps**: `last_ping_protocol_message_received_at` should only be updated on *pong* receipt (i.e., in response to a ping the victim initiated), not on receipt of an unsolicited incoming ping. This prevents an attacker from refreshing the timestamp by spamming pings. [4](#0-3) 

3. **Add per-IP or per-/16-subnet inbound connection limits** upstream of the eviction path to raise the cost of establishing 16 simultaneous inbound connections.

4. **Add a network-group diversity check** to the protection tiers so that a single network group cannot fill an entire protection tier.

---

### Proof of Concept

```rust
// Pseudocode differential test
let mut registry = PeerRegistry::new(17, 3, false, vec![], true);

// Attacker group A: 8 peers, ping_rtt = Duration::from_millis(500) → as_secs() == 0
for i in 0..8 {
    registry.accept_peer(attacker_addr_a(i), session_a(i), Inbound, &mut store);
    registry.get_peer_mut(session_a(i)).unwrap().ping_rtt = Some(Duration::from_millis(500));
}

// Attacker group B: 8 peers, last_ping_protocol_message_received_at = now
for i in 0..8 {
    registry.accept_peer(attacker_addr_b(i), session_b(i), Inbound, &mut store);
    registry.get_peer_mut(session_b(i)).unwrap()
        .last_ping_protocol_message_received_at = Some(Instant::now());
}

// 1 honest peer: no ping data, no recent messages
registry.accept_peer(honest_addr(), session_honest(), Inbound, &mut store);

// Now trigger eviction 10000 times; honest peer is evicted every time
for _ in 0..10000 {
    let evicted = registry.try_evict_inbound_peer(&store);
    assert_eq!(evicted, Some(session_honest())); // always true
}
```

The honest peer is evicted with 100% probability because after Tier 1 removes attacker group A and Tier 2 removes attacker group B, only the honest peer remains as a candidate. [8](#0-7)

### Citations

**File:** network/src/peer_registry.rs (L55-63)
```rust
fn sort_then_drop<T, F>(list: &mut Vec<T>, n: usize, compare: F)
where
    F: FnMut(&T, &T) -> std::cmp::Ordering,
{
    list.sort_by(compare);
    if list.len() > n {
        list.truncate(list.len() - n);
    }
}
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

**File:** network/src/protocols/ping.rs (L62-68)
```rust
    fn ping_received(&mut self, id: SessionId) {
        trace!("received ping from: {:?}", id);
        self.network_state.with_peer_registry_mut(|reg| {
            if let Some(peer) = reg.get_peer_mut(id) {
                peer.last_ping_protocol_message_received_at = Some(Instant::now());
            }
        });
```

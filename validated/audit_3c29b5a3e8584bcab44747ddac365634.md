### Title
Eviction Protection Bypass via `as_secs()` Granularity and Unsolicited Ping Manipulation — (`network/src/peer_registry.rs`, `network/src/protocols/ping.rs`)

### Summary

`try_evict_inbound_peer` uses `.as_secs()` (whole-second truncation) when comparing `ping_rtt` and `last_ping_protocol_message_received_at`. Any peer with RTT < 1 s maps to the same score (0). Because `ping_received` also updates `last_ping_protocol_message_received_at` on **unsolicited** inbound Pings, an attacker can simultaneously occupy both protection slots for all their sessions. When `max_inbound ≤ 2 × EVICTION_PROTECT_PEERS` (= 16), the candidate pool is fully exhausted and `try_evict_inbound_peer` returns `None`, permanently blocking honest peers.

---

### Finding Description

**1. `ping_rtt` protection uses `.as_secs()` — coarse truncation** [1](#0-0) 

`p.as_secs()` truncates any Duration < 1 s to `0`. Every peer that responds to a Ping within one second — including all honest peers on a well-connected network — receives the same score. The sort is therefore arbitrary among all sub-second-RTT peers; the "lowest ping" protection degenerates to a random selection.

**2. `last_ping_protocol_message_received_at` is updated by unsolicited inbound Pings** [2](#0-1) 

There is no rate-limit or solicitation check. Any remote peer can send a Ping at any time to refresh this timestamp to `Instant::now()`. The "most recent message" protection slot is therefore trivially manipulable.

**3. Pong nonce validation does not prevent near-zero RTT** [3](#0-2) 

The nonce is `elapsed_secs_since_start as u32` — it changes only once per second. [4](#0-3) 

An attacker that responds within the same second always supplies the correct nonce, so `pong_received` is called and `ping_rtt` is set to the actual (near-zero) elapsed time.

**4. `sort_then_drop` exhausts the candidate pool when `max_inbound ≤ 16`** [5](#0-4) 

With `EVICTION_PROTECT_PEERS = 8`: [6](#0-5) 

- Step 1 (ping): protects 8 → `max_inbound − 8` remain
- Step 2 (last message): protects 8 → `max_inbound − 16` remain
- Step 3 (connection time): protects half of remainder

When `max_inbound ≤ 16`, after steps 1 and 2 the candidate list is empty (`len = 0`). Step 3 computes `protect_peers = 0 >> 1 = 0` and does nothing. `evict_group` is empty; `evict_group.choose(...)` returns `None`. [7](#0-6) 

`accept_peer` then returns `Err(PeerError::ReachMaxInboundLimit)` for every subsequent honest connection attempt. [8](#0-7) 

---

### Impact Explanation

When `max_inbound ≤ 16`, an attacker who fills all inbound slots with sessions that (a) respond to Pings in < 1 s and (b) send unsolicited Pings continuously causes `try_evict_inbound_peer` to permanently return `None`. No honest peer can ever obtain an inbound slot — complete eclipse of inbound connections.

For larger `max_inbound` (e.g., 125), the attack degrades but does not fully prevent eviction: after two rounds of 8 protections and halving the remainder, ~55 attacker sessions remain as candidates and one will eventually be evicted. However, the `as_secs()` granularity flaw still makes the ping-based protection meaningless for any sub-second-RTT peer.

---

### Likelihood Explanation

- Filling ≤ 16 inbound slots requires only 16 TCP connections from distinct peer IDs; no PoW, no key material, no privileged access.
- Responding to Pings in < 1 s is trivially achievable on any modern host.
- Sending unsolicited Pings is a single message per second per session.
- The attack is fully local-testable with a single machine.

---

### Recommendation

1. **Replace `.as_secs()` with `.as_millis()` or `.as_micros()`** in both comparators inside `try_evict_inbound_peer` so that sub-second RTT differences are meaningful.
2. **Ignore unsolicited inbound Pings** for the purpose of updating `last_ping_protocol_message_received_at`, or use a separate field (e.g., `last_block_or_tx_message_received_at`) that cannot be refreshed by the Ping protocol alone.
3. **Enforce a minimum candidate pool size** before returning from `try_evict_inbound_peer`, or increase `EVICTION_PROTECT_PEERS` only when the pool is large enough to leave at least one unprotected candidate.
4. Consider rate-limiting inbound Ping messages per session.

---

### Proof of Concept

```
1. Start a CKB node with max_inbound = 16.
2. Connect 16 attacker sessions (distinct peer IDs, any IPs).
3. For each attacker session:
   a. On receiving a Ping from the node, immediately reply with Pong(same_nonce).
      → ping_rtt = ~0 µs → as_secs() = 0
   b. Every 500 ms, send an unsolicited Ping to the node.
      → last_ping_protocol_message_received_at = now → as_secs() = 0
4. Attempt to connect a 17th (honest) peer.
5. try_evict_inbound_peer:
   - 16 candidates, all ping_rtt.as_secs()=0 → sort is arbitrary, drop last 8 → 8 remain
   - 8 candidates, all last_message.as_secs()=0 → sort is arbitrary, drop last 8 → 0 remain
   - protect_peers = 0>>1 = 0 → evict_group is empty → returns None
6. accept_peer returns Err(ReachMaxInboundLimit) → honest peer rejected.
7. Repeat step 4 indefinitely: honest peer is permanently blocked.
```

### Citations

**File:** network/src/peer_registry.rs (L17-17)
```rust
pub(crate) const EVICTION_PROTECT_PEERS: usize = 8;
```

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

**File:** network/src/peer_registry.rs (L116-121)
```rust
                if connection_status.non_whitelist_inbound >= self.max_inbound {
                    if let Some(evicted_session) = self.try_evict_inbound_peer(peer_store) {
                        evicted_peer = self.remove_peer(evicted_session);
                    } else {
                        return Err(PeerError::ReachMaxInboundLimit.into());
                    }
```

**File:** network/src/peer_registry.rs (L155-164)
```rust
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

**File:** network/src/protocols/ping.rs (L117-119)
```rust
fn nonce(t: &Instant, start_time: Instant) -> u32 {
    t.saturating_duration_since(start_time).as_secs() as u32
}
```

**File:** network/src/protocols/ping.rs (L225-234)
```rust
                    PingPayload::Pong(nonce) => {
                        // check pong
                        if let Some(status) = self.connected_session_ids.get_mut(&session.id)
                            && (true, nonce) == (status.processing, status.nonce())
                        {
                            status.processing = false;
                            let last_ping_sent_at = status.last_ping_sent_at;
                            self.pong_received(session.id, last_ping_sent_at);
                            return;
                        }
```

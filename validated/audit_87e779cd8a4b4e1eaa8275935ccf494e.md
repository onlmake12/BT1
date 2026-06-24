All code claims check out against the actual source. Let me verify each assertion:

**`ping_received()` — no guard on unsolicited Pings:** [1](#0-0) [2](#0-1) 

Any inbound `Ping` unconditionally writes `Instant::now()` to `last_ping_protocol_message_received_at`. No prior-outbound-Ping check, no rate limit.

**Second eviction pass reads that field:** [3](#0-2) 

Sort is `peer2_last_message.cmp(&peer1_last_message)` (descending elapsed time). `sort_then_drop` keeps the first `len - n` elements and removes the last `n` — so the `n` peers with the *smallest* elapsed time (most recently active) are removed from the candidate list, i.e., protected.

**`sort_then_drop` semantics confirmed:** [4](#0-3) 

**Timeout path requires `processing = true`, set only on outbound Ping:** [5](#0-4) [6](#0-5) 

Receiving inbound Pings never sets `processing = true`, so the timeout disconnection path is never triggered for the attacker.

**First pass does not protect the attacker (no `ping_rtt`):** [7](#0-6) 

`ping_rtt` is only set in `pong_received()`, not `ping_received()`. An attacker sending only Pings has `ping_rtt = None → u64::MAX`, placing them at the front of the descending sort — not in the protected tail.

**Eviction triggered at max inbound:** [8](#0-7) 

**Stated invariant is false:** [9](#0-8) 

All five technical sub-claims are confirmed by the actual code. The exploit path is complete and requires no privileges.

---

Audit Report

## Title
Unsolicited Ping Flood Bypasses Eviction Protection to Permanently Hold Inbound Slot — (`network/src/protocols/ping.rs`, `network/src/peer_registry.rs`)

## Summary
`ping_received()` unconditionally updates `last_ping_protocol_message_received_at` for any inbound `Ping` message, with no rate-limit and no requirement that the node first sent a `Ping`. The second pass of `try_evict_inbound_peer()` protects the 8 peers whose `last_ping_protocol_message_received_at` is most recent. An attacker who continuously sends unsolicited `Ping` messages keeps their timestamp perpetually fresh, ensuring they are always among the 8 protected peers and are never selected for eviction, permanently occupying an inbound slot.

## Finding Description
**Root cause — `ping_received()` writes timestamp on any inbound Ping:**

```rust
// ping.rs L62-69
fn ping_received(&mut self, id: SessionId) {
    self.network_state.with_peer_registry_mut(|reg| {
        if let Some(peer) = reg.get_peer_mut(id) {
            peer.last_ping_protocol_message_received_at = Some(Instant::now());
        }
    });
}
```

Called unconditionally at L216 for every `PingPayload::Ping`. No guard checks whether the node sent a `Ping` first, no rate-limit, no counter.

**Eviction protection reads the same field:**

The second `sort_then_drop` pass in `try_evict_inbound_peer()` (L167–183) sorts candidates by descending elapsed time since `last_ping_protocol_message_received_at` and removes the 8 with the smallest elapsed time (most recently active) from the candidate pool. An attacker flooding Pings always has elapsed ≈ 0, placing them in the protected tail.

**Timeout path is inert for the attacker:**

`CHECK_TIMEOUT_TOKEN` disconnects only peers where `ps.processing && ps.elapsed() >= timeout`. `processing` is set exclusively in `ping_peers()` when the node sends outbound Pings. Receiving inbound Pings never sets `processing = true`, so the attacker is never disconnected by the timeout path.

**First pass does not protect the attacker:**

`ping_rtt` is set only in `pong_received()`. An attacker sending only Pings has `ping_rtt = None → u64::MAX`, placing them at the front of the first sort (worst RTT), so they are not protected by the first pass and remain in the candidate pool until the second pass protects them via the timestamp flood.

**Stated invariant is violated:**

The comment at L149 claims protection is based on "characteristics that an attacker hard to simulate or manipulate." The second criterion (`last_ping_protocol_message_received_at`) is trivially manipulable via unsolicited Pings.

## Impact Explanation
When `max_inbound` is reached and a legitimate peer attempts to connect, `accept_peer()` calls `try_evict_inbound_peer()`. The attacker's peer is always among the 8 most recently active and is removed from the eviction candidate set. If multiple coordinated attackers occupy the second-pass protection slots (up to 8), combined with the first-pass and third-pass protections, the candidate pool can be exhausted, causing `try_evict_inbound_peer()` to return `None` and every new legitimate peer to receive `PeerError::ReachMaxInboundLimit`. This maps to **High: Vulnerabilities or bad designs which could cause CKB network congestion with few costs** — the cost is a standard P2P connection plus a tight loop of 4-byte Ping messages.

## Likelihood Explanation
The attack requires only a valid P2P connection and the ability to send `Ping` messages in a loop. The `Ping` message format is public (a 4-byte nonce). No cryptographic material, hashpower, or privileged access is needed. The attack is repeatable, persistent, and trivially automated. Multiple attackers coordinating to fill all 8 second-pass protection slots further amplifies the impact.

## Recommendation
1. **Only update `last_ping_protocol_message_received_at` in `pong_received()`**, not in `ping_received()`. A valid `Pong` must echo the correct nonce from a node-initiated `Ping`, making it unforgeable without a prior outbound `Ping`.
2. **Rate-limit inbound `Ping` messages** per session (e.g., one per interval) to prevent flooding regardless of the timestamp update logic.
3. Alternatively, rename/split the field so the eviction criterion tracks only genuine round-trip activity (Pong receipts), not unsolicited inbound Pings.

## Proof of Concept
```
1. Node N has max_inbound = K; all K slots filled with legitimate peers.
2. Attacker A connects as inbound peer, displacing one legitimate peer.
3. A enters a tight loop: send Ping(nonce=any) repeatedly.
   - Each Ping triggers ping_received() → last_ping_protocol_message_received_at = Instant::now().
   - No rate-limit stops this loop.
4. A new legitimate peer L attempts to connect → accept_peer() → try_evict_inbound_peer().
5. Second sort_then_drop protects the 8 most recently active peers; A's elapsed ≈ 0 → always in protected tail.
6. A is never selected for eviction; L receives ReachMaxInboundLimit.

Invariant/fuzz test:
- Fill max_inbound slots; have 8 of them continuously send Pings.
- Assert try_evict_inbound_peer() returns None (no evictable candidate after all protection passes).
- Confirm legitimate peers are rejected with ReachMaxInboundLimit.
```

### Citations

**File:** network/src/protocols/ping.rs (L62-69)
```rust
    fn ping_received(&mut self, id: SessionId) {
        trace!("received ping from: {:?}", id);
        self.network_state.with_peer_registry_mut(|reg| {
            if let Some(peer) = reg.get_peer_mut(id) {
                peer.last_ping_protocol_message_received_at = Some(Instant::now());
            }
        });
    }
```

**File:** network/src/protocols/ping.rs (L86-96)
```rust
            .iter_mut()
            .filter_map(|(session_id, ps)| {
                if ps.processing {
                    None
                } else {
                    ps.processing = true;
                    ps.last_ping_sent_at = now;
                    ps.nonce = send_nonce;
                    Some(*session_id)
                }
            })
```

**File:** network/src/protocols/ping.rs (L215-216)
```rust
                    PingPayload::Ping(nonce) => {
                        self.ping_received(session.id);
```

**File:** network/src/protocols/ping.rs (L254-268)
```rust
            CHECK_TIMEOUT_TOKEN => {
                let timeout = self.timeout;
                for (id, _ps) in self
                    .connected_session_ids
                    .iter()
                    .filter(|(_id, ps)| ps.processing && ps.elapsed() >= timeout)
                {
                    debug!("Ping timeout, {:?}", id);
                    if let Err(err) =
                        async_disconnect_with_message(context.control(), *id, "ping timeout").await
                    {
                        debug!("Disconnect failed {:?}, error: {:?}", id, err);
                    }
                }
            }
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

**File:** network/src/peer_registry.rs (L115-121)
```rust
            if session_type.is_inbound() {
                if connection_status.non_whitelist_inbound >= self.max_inbound {
                    if let Some(evicted_session) = self.try_evict_inbound_peer(peer_store) {
                        evicted_peer = self.remove_peer(evicted_session);
                    } else {
                        return Err(PeerError::ReachMaxInboundLimit.into());
                    }
```

**File:** network/src/peer_registry.rs (L149-150)
```rust
        // Protect peers based on characteristics that an attacker hard to simulate or manipulate
        // Protect peers which has the lowest ping
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

**File:** network/src/peer_registry.rs (L167-183)
```rust
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
```

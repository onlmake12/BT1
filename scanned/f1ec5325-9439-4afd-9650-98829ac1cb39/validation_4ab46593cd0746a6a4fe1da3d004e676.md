Audit Report

## Title
Unsolicited Ping messages allow an inbound peer to trivially bypass eviction protection — (`network/src/protocols/ping.rs`, `network/src/peer_registry.rs`)

## Summary
`PingHandler::received()` calls `ping_received()` unconditionally for every inbound `PingPayload::Ping`, which sets `peer.last_ping_protocol_message_received_at = Some(Instant::now())` with no rate limit or prior-challenge requirement. The eviction logic in `try_evict_inbound_peer()` uses this field to protect the 8 most-recently-active peers from the candidate pool, making the protection criterion freely attacker-controlled. An attacker holding a single inbound slot can guarantee permanent residency by sending periodic Ping messages, displacing honest peers at negligible cost.

## Finding Description
**Root cause — `ping_received()` updates the eviction-relevant field on any inbound Ping:**

`network/src/protocols/ping.rs` lines 62–69 set `last_ping_protocol_message_received_at` to `Instant::now()` for any peer that sends a `Ping`, with no validation: [1](#0-0) 

This is called unconditionally from `received()` at line 216 before any nonce or challenge check: [2](#0-1) 

Contrast with `pong_received()` (lines 71–79), which only updates the field after validating `status.processing && nonce == status.nonce()` — a genuine bidirectional liveness check. The Ping path has no equivalent guard. [3](#0-2) 

**Eviction logic consumes the attacker-controlled field:**

`try_evict_inbound_peer()` runs three protection rounds. Round 2 (lines 167–183) sorts candidates by elapsed time since `last_ping_protocol_message_received_at` in descending order (`peer2_last_message.cmp(&peer1_last_message)` = largest elapsed first), then calls `sort_then_drop` with `n = EVICTION_PROTECT_PEERS (8)`: [4](#0-3) 

`sort_then_drop` truncates the list to `len - n`, removing the last `n` elements — those with the *smallest* elapsed time (most recently active): [5](#0-4) 

Because the attacker can send a Ping at any time to reset their elapsed time to ~0 s, they will always appear among the 8 most-recently-active peers and be dropped from the candidate pool before any eviction decision is made.

The code's own comment at line 149 states the invariant being violated: [6](#0-5) 

Round 1 (lowest `ping_rtt`) reflects actual measured network latency and cannot be freely spoofed. Round 3 (longest `connected_time`) is set at connection time and is immutable. Round 2 provides no such resistance.

## Impact Explanation
At `max_inbound` capacity, every new legitimate peer triggers `try_evict_inbound_peer()`. The attacker's peer is removed from the candidate pool in round 2 (elapsed ≈ 0 s), so a legitimate long-lived peer is evicted instead. Repeated over time, an attacker with even a single connection can guarantee permanent residency. With multiple connections from distinct `/16` network groups (to survive the final group-based random eviction step), an attacker can occupy a disproportionate share of inbound slots at negligible cost — one small Ping message per interval per connection. This maps to **High: Vulnerabilities or bad designs which could cause CKB network congestion with few costs**, as it allows an attacker to systematically displace honest peers from inbound slots, degrading the node's ability to maintain a diverse, honest peer set.

## Likelihood Explanation
The attack requires only a standard TCP connection and the ability to send small Ping protocol messages (a 4-byte nonce in a flatbuffer envelope). No proof-of-work, no key material, no privileged access is needed. The Ping message is tiny and the required send rate (e.g., one per 100 ms) imposes negligible bandwidth cost. The attack is fully local-testable and requires no external coordination.

## Recommendation
1. **Do not update `last_ping_protocol_message_received_at` on receipt of an unsolicited `Ping`.** Only update it on receipt of a valid `Pong` that matches a nonce the local node sent — i.e., inside `pong_received()`, which already validates `status.processing && nonce == status.nonce()`. This makes the field reflect genuine bidirectional liveness, not attacker-controlled activity.
2. Alternatively, add per-session rate limiting on inbound Ping messages so that flooding cannot refresh the timestamp faster than the eviction interval.
3. Consider replacing the `last_ping_protocol_message_received_at` eviction criterion with a metric that is harder to manipulate, such as the number of valid block or header announcements received from the peer.

## Proof of Concept
```
1. Connect max_inbound honest peers to a test node.
2. Connect one attacker peer.
3. Attacker sends a Ping message every 100 ms in a loop.
4. Attempt to connect a new legitimate peer repeatedly.
5. Observe: try_evict_inbound_peer() always protects the attacker in round 2
   (smallest elapsed since last_ping_protocol_message_received_at ≈ 0 s),
   evicting an honest peer instead.
6. Assert: attacker session_id is never selected for eviction;
   legitimate peer always succeeds by displacing an honest peer.
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

**File:** network/src/protocols/ping.rs (L71-79)
```rust
    fn pong_received(&mut self, id: SessionId, last_ping: Instant) {
        let now = Instant::now();
        self.network_state.with_peer_registry_mut(|reg| {
            if let Some(peer) = reg.get_peer_mut(id) {
                peer.ping_rtt = Some(now.saturating_duration_since(last_ping));
                peer.last_ping_protocol_message_received_at = Some(now);
            }
        });
    }
```

**File:** network/src/protocols/ping.rs (L214-219)
```rust
                match msg {
                    PingPayload::Ping(nonce) => {
                        self.ping_received(session.id);
                        if context
                            .send_message(PingMessage::build_pong(nonce))
                            .await
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

**File:** network/src/peer_registry.rs (L149-150)
```rust
        // Protect peers based on characteristics that an attacker hard to simulate or manipulate
        // Protect peers which has the lowest ping
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

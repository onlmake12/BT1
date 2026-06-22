### Title
Unsolicited Ping Flooding Bypasses Recency-Protection in `try_evict_inbound_peer` — (`network/src/protocols/ping.rs`, `network/src/peer_registry.rs`)

### Summary

`PingHandler::ping_received()` unconditionally updates `Peer::last_ping_protocol_message_received_at` to `Instant::now()` for every inbound Ping message, with no rate limiting. The `try_evict_inbound_peer()` recency-protection pass uses this field to protect the 8 most recently active peers from eviction. An attacker sending cheap, unsolicited Ping messages at high frequency can permanently keep their peer in the protected set, squatting an inbound slot indefinitely.

---

### Finding Description

**Entrypoint — `ping_received()` called unconditionally on every Ping:**

In `network/src/protocols/ping.rs`, the `received()` handler dispatches on message type. For `PingPayload::Ping`, it calls `ping_received()` with no rate check: [1](#0-0) 

`ping_received()` then unconditionally stamps the peer's activity field: [2](#0-1) 

There is no counter, no per-session rate limit, and no minimum interval enforced anywhere in `ping.rs`. A grep for `rate_limit`, `throttle`, or `flood` in `network/src/protocols/ping.rs` returns zero matches.

**Vulnerable field — `last_ping_protocol_message_received_at` in `Peer`:** [3](#0-2) 

The field's doc comment says "Ping/Pong message last received time", but the update path in `ping_received()` fires on one-sided Ping messages, not only on completed Ping→Pong round-trips.

**Eviction protection pass reads this field directly:**

In `try_evict_inbound_peer()`, the second `sort_then_drop` pass protects `EVICTION_PROTECT_PEERS` (8) peers with the smallest elapsed time since `last_ping_protocol_message_received_at`: [4](#0-3) 

`sort_then_drop` sorts ascending by elapsed time (most-recent last) and truncates the front, so the 8 most-recently-stamped peers are removed from the eviction candidate list — i.e., protected: [5](#0-4) 

An attacker sending a Ping every 100 ms keeps their elapsed time at ~0 s, always landing in the protected tail.

---

### Impact Explanation

The attacker permanently occupies one (or more, with multiple connections) eviction-protected inbound slots. Legitimate peers that are genuinely idle between normal Ping intervals (~15 s) accumulate a larger elapsed time and are preferentially evicted instead. This:

- Reduces effective inbound capacity for honest peers.
- Lets the attacker maintain a persistent, uninterruptible inbound connection useful for traffic analysis, eclipse-attack staging, or other follow-on attacks.

---

### Likelihood Explanation

The exploit requires only a valid TCP connection and the ability to send well-formed Ping messages — both trivially achievable by any unprivileged peer. No PoW, no key material, no privileged role is needed. The cost is negligible (a few bytes per 100 ms). There is no existing guard in the production code path.

---

### Recommendation

1. **Separate Ping-received from Pong-received for the activity timestamp.** Only update `last_ping_protocol_message_received_at` (or rename it to `last_pong_received_at`) inside `pong_received()`, which requires a valid nonce matching an outstanding outbound Ping — a property the attacker cannot forge without the victim first initiating the exchange.

2. **Rate-limit inbound Ping messages per session.** Enforce a minimum inter-Ping interval (e.g., no more than one Ping per `interval/2`) and disconnect peers that exceed it.

---

### Proof of Concept

```
1. Victim node: max_inbound = N, all N slots filled with legitimate peers.
2. Attacker connects (evicts one legitimate peer to get a slot).
3. Attacker spawns a loop: every 100 ms, send a valid PingMessage::Ping(nonce).
4. Each message triggers ping_received() → last_ping_protocol_message_received_at = Instant::now().
5. When any new peer tries to connect, try_evict_inbound_peer() runs.
6. Recency pass: attacker's elapsed ≈ 0 s; legitimate peers' elapsed ≈ 0–15 s.
7. Attacker is always in the protected-8 tail; a legitimate peer is evicted instead.
8. Repeat indefinitely — attacker's slot is never reclaimed.
```

The existing test `test_accept_inbound_peer_eviction` in `network/src/tests/peer_registry.rs` (lines 120–227) already demonstrates the recency-protection mechanism; extending it to simulate a peer with `last_ping_protocol_message_received_at = Instant::now()` updated every 100 ms would confirm the attacker is never selected across 1 000 eviction rounds.

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

**File:** network/src/protocols/ping.rs (L214-218)
```rust
                match msg {
                    PingPayload::Ping(nonce) => {
                        self.ping_received(session.id);
                        if context
                            .send_message(PingMessage::build_pong(nonce))
```

**File:** network/src/peer.rs (L71-71)
```rust
    pub last_ping_protocol_message_received_at: Option<Instant>,
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

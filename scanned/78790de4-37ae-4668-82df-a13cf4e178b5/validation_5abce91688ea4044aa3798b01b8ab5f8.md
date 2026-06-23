### Title
Attacker-Controlled `last_ping_protocol_message_received_at` Bypasses Eviction Protection Intent — (`network/src/protocols/ping.rs`, `network/src/peer_registry.rs`)

---

### Summary

An unprivileged inbound peer can send unsolicited Ping messages to continuously refresh `last_ping_protocol_message_received_at`, gaining preferential protection in the eviction algorithm. This directly contradicts the stated design invariant and allows an attacker to bias eviction against legitimate peers.

---

### Finding Description

`ping_received()` updates `last_ping_protocol_message_received_at` unconditionally on any incoming Ping message, with no rate limiting: [1](#0-0) 

This field is then used in `try_evict_inbound_peer()` to protect the `EVICTION_PROTECT_PEERS` (8) most recently active peers from eviction: [2](#0-1) 

The code comment at line 149 explicitly states the design intent: [3](#0-2) 

> "Protect peers based on characteristics that an attacker **hard to simulate or manipulate**"

But `last_ping_protocol_message_received_at` is trivially manipulable — any connected peer can send Ping messages at will to keep this timestamp at `Instant::now()`.

The `sort_then_drop` helper sorts candidates and removes the last `n` (most recently active) from the eviction pool: [4](#0-3) 

The eviction algorithm has three protection rounds:
1. **Round 1 (ping RTT)**: Protects 8 peers with lowest `ping_rtt`. Attacker peers that only send Ping (never respond to Pong) have `ping_rtt = None` → mapped to `u64::MAX` → sorted first → **NOT protected**, remain as candidates.
2. **Round 2 (recent activity)**: Protects 8 peers with smallest `last_ping_protocol_message_received_at` duration. Attacker peers flooding Pings have duration ≈ 0 → sorted last → **protected and removed from candidates**.
3. **Round 3 (connection time)**: Protects half of remaining candidates with longest connection time.

The attacker's peers survive to Round 2 (because they have no ping_rtt) and are then shielded there by Ping flooding. Legitimate peers that also lack ping_rtt but are not flooding Pings are left as the eviction targets.

The `Peer` struct initializes `last_ping_protocol_message_received_at` as `None`: [5](#0-4) [6](#0-5) 

A peer with `None` maps to `u64::MAX` duration — the worst possible score — making it a prime eviction target. The attacker avoids this by flooding Pings.

---

### Impact Explanation

With 8+ attacker-controlled inbound connections all sending Ping floods:
- All 8 "recent activity" protection slots in Round 2 are occupied by attacker peers.
- Legitimate peers with no recent Ping activity (or `None` timestamp) are left as eviction candidates.
- When a new legitimate peer attempts to connect, the eviction algorithm preferentially removes a legitimate peer rather than an attacker peer.
- The attacker can immediately reconnect any evicted peer, maintaining slot dominance.
- Net effect: attacker peers persistently occupy a disproportionate share of inbound slots, degrading network topology and reducing the victim node's connectivity to honest peers.

---

### Likelihood Explanation

The exploit requires only:
1. The ability to open 8+ inbound TCP connections to the target node (no privilege, no PoW, no key).
2. Sending Ping messages in a loop — a trivial P2P operation using the standard CKB ping protocol format.

There is no rate limiting, no nonce validation for incoming Pings (only for Pong), and no cap on how frequently `last_ping_protocol_message_received_at` can be updated. The path is fully reachable from the P2P network layer.

---

### Recommendation

1. **Only update `last_ping_protocol_message_received_at` on valid Pong responses**, not on incoming Ping messages. The field name and eviction comment both imply it should reflect genuine bidirectional protocol participation. Move the timestamp update out of `ping_received()` and keep it only in `pong_received()`.
2. **Add rate limiting** on incoming Ping messages per session (e.g., one Ping per interval window) to prevent flooding.
3. Consider renaming the field to `last_pong_received_at` to make the intended semantics explicit and prevent future regressions.

---

### Proof of Concept

```
1. Attacker opens 8 inbound connections to victim node (fills EVICTION_PROTECT_PEERS slots).
2. Each attacker peer sends Ping messages in a tight loop:
      PingMessage::build_ping(any_nonce) → send repeatedly
3. Each Ping triggers ping_received() → last_ping_protocol_message_received_at = Instant::now()
4. A legitimate peer (session 9) attempts to connect → max_inbound reached → try_evict_inbound_peer() called.
5. Round 1: all 8 attacker peers have ping_rtt=None (u64::MAX) → not protected, remain candidates.
6. Round 2: all 8 attacker peers have last_ping_protocol_message_received_at ≈ now (duration ≈ 0) → protected, removed from candidates.
7. Remaining candidates: legitimate peers with stale or None timestamps → one is evicted.
8. Attacker's evicted peer (if any) reconnects immediately and resumes Ping flooding.
9. Assert: no attacker peer is ever chosen for final eviction when ≥8 attacker peers are active.
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

**File:** network/src/peer.rs (L71-71)
```rust
    pub last_ping_protocol_message_received_at: Option<Instant>,
```

**File:** network/src/peer.rs (L103-103)
```rust
            last_ping_protocol_message_received_at: None,
```

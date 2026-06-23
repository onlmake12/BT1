The bug claim is verifiable. Let me confirm the exact code.

The eviction comparator in `network/src/peer_registry.rs` lines 154–164 uses `.as_secs()`:

```rust
let peer1_ping = peer1
    .ping_rtt
    .map(|p| p.as_secs())          // truncates to whole seconds
    .unwrap_or_else(|| u64::MAX);
let peer2_ping = peer2
    .ping_rtt
    .map(|p| p.as_secs())
    .unwrap_or_else(|| u64::MAX);
peer2_ping.cmp(&peer1_ping)
```

`pong_received` stores the full-precision `Duration`:

```rust
peer.ping_rtt = Some(now.saturating_duration_since(last_ping));
```

So a peer with RTT = 10 ms and a peer with RTT = 999 ms both produce `as_secs() == 0` and are treated identically by the sort. The existing test in `network/src/tests/peer_registry.rs` line 171 even sets `ping_rtt = Some(Duration::from_secs(0))` — it never tests sub-second discrimination, so the regression is untested.

---

### Title
Eviction Protection Bypass via Sub-Second RTT Truncation in `try_evict_inbound_peer` — (`network/src/peer_registry.rs`)

### Summary
`try_evict_inbound_peer` compares `ping_rtt` values using `.as_secs()`, which truncates any RTT below 1 second to zero. Because virtually all real-world peer RTTs are sub-second (10 ms – 900 ms), every peer with a measured RTT maps to the same value (0), making the "protect lowest-ping peers" sort a no-op. An attacker who responds to pings immediately (RTT ≈ 0 ms → 0 s) is indistinguishable from a legitimate peer with RTT = 900 ms, defeating the intended protection.

### Finding Description
In `network/src/peer_registry.rs`, `try_evict_inbound_peer` sorts candidate peers to protect the `EVICTION_PROTECT_PEERS` lowest-ping connections from eviction: [1](#0-0) 

The comparator calls `.as_secs()` on the stored `Duration`. `Duration::as_secs()` returns only the integer-second component, discarding all sub-second precision. For any RTT in the range [0 ms, 999 ms], the result is `0`. Since typical internet RTTs are well under 1 second, all peers with a measured RTT sort to the same key and the protection step provides no ordering guarantee.

The RTT is stored with full precision by `pong_received`: [2](#0-1) 

The attacker's entry point is a standard inbound P2P connection. Upon receiving a `Ping` message, the attacker's node replies with the matching `Pong` immediately. The nonce check at line 228 only validates that the nonce matches — it does not bound the response time: [3](#0-2) 

This yields `ping_rtt ≈ Some(Duration::from_micros(N))`, which `.as_secs()` maps to `0` — identical to a legitimate peer with RTT = 900 ms.

### Impact Explanation
The "protect lowest-ping peers" invariant is completely neutralised for any deployment where all peers have sub-second RTTs (i.e., all real deployments). An attacker controlling multiple inbound connections can ensure their connections are never disadvantaged by the ping-protection step, increasing the probability that legitimate well-connected peers are evicted instead. This weakens the node's resistance to inbound-slot exhaustion and eclipse-attack scenarios.

### Likelihood Explanation
The attack requires only the ability to open inbound connections to the target node and respond to pings quickly — both trivially achievable. No special privileges, keys, or majority hashpower are needed. The other eviction layers (recent-message time, connection age) use the same `.as_secs()` truncation for the message-time comparison, compounding the issue. [4](#0-3) 

### Recommendation
Replace `.as_secs()` with `.as_millis()` (or `.as_nanos()`) in all three `sort_then_drop` comparators inside `try_evict_inbound_peer` so that sub-second differences are preserved. The fix is a one-line change per comparator.

### Proof of Concept
1. Populate a `PeerRegistry` with 20 inbound peers, assigning `ping_rtt` values of 10 ms, 50 ms, 100 ms, … 900 ms (all sub-second).
2. Add one attacker peer with `ping_rtt = Some(Duration::from_micros(100))` (≈ 0 ms).
3. Call `try_evict_inbound_peer`.
4. With the current `.as_secs()` code, all 21 peers sort to key `0` and the eviction is uniformly random — the attacker peer has the same eviction probability as the 10 ms legitimate peer, defeating the protection.
5. With `.as_millis()`, the 10 ms peer sorts to key `10` and is correctly protected; the attacker peer (key `0`) is correctly left unprotected.

The existing test at `network/src/tests/peer_registry.rs` line 171 sets `ping_rtt = Some(Duration::from_secs(0))` and never exercises sub-second discrimination, so this regression is not caught by the test suite. [5](#0-4)

### Citations

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

**File:** network/src/peer_registry.rs (L173-183)
```rust
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

**File:** network/src/protocols/ping.rs (L225-233)
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
```

**File:** network/src/tests/peer_registry.rs (L163-173)
```rust
    // lowest ping peers
    for _ in 0..EVICTION_PROTECT_PEERS {
        let peer_addr = peers_iter.next().unwrap();
        let peer_id = extract_peer_id(peer_addr).unwrap();
        let session_id = peers_registry
            .get_key_by_peer_id(&peer_id)
            .expect("get_key_by_peer_id failed");
        if let Some(peer) = peers_registry.get_peer_mut(session_id) {
            peer.ping_rtt = Some(Duration::from_secs(0));
        };
    }
```

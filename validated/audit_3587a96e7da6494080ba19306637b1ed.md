Looking at the exact code paths:

### Title
Unsolicited Ping Flood Manipulates Eviction Protection to Permanently Occupy Inbound Slot — (`network/src/protocols/ping.rs`, `network/src/peer_registry.rs`)

---

### Summary

An unprivileged inbound peer can send a continuous stream of unsolicited `Ping` messages to keep its `last_ping_protocol_message_received_at` timestamp perpetually fresh, causing `try_evict_inbound_peer()` to always classify it as "most recently active" and protect it from eviction. This directly contradicts the code's own stated invariant and allows the attacker to permanently hold an inbound slot.

---

### Finding Description

**Entrypoint — `ping_received()` updates timestamp on any inbound Ping:**

In `network/src/protocols/ping.rs`, the `received()` dispatch at line 215 calls `ping_received()` for every `PingPayload::Ping` message, regardless of whether the node ever solicited one:

```rust
PingPayload::Ping(nonce) => {
    self.ping_received(session.id);   // ← no check that we sent a Ping first
    ...
}
```

`ping_received()` unconditionally writes `Instant::now()` to the peer's registry entry: [1](#0-0) 

There is no rate-limit, no guard requiring a prior outbound Ping, and no counter.

**Eviction protection reads that same field:**

`try_evict_inbound_peer()` in `network/src/peer_registry.rs` runs three sequential `sort_then_drop` passes. The second pass (lines 167–183) protects the `EVICTION_PROTECT_PEERS` (= 8) peers whose `last_ping_protocol_message_received_at` is most recent: [2](#0-1) 

`sort_then_drop` sorts ascending by age (oldest first) and then truncates the tail — the 8 peers with the *smallest* elapsed time are removed from the eviction candidate list (i.e., protected): [3](#0-2) 

The comment on line 149 explicitly claims this protection is based on "characteristics that an attacker [is] hard to simulate or manipulate": [4](#0-3) 

That claim is false for the second criterion.

**Why the ping-timeout mechanism does not evict the attacker:**

The `CHECK_TIMEOUT_TOKEN` handler disconnects peers only when `ps.processing && ps.elapsed() >= timeout`. The `processing` flag is set exclusively when the *node* sends a Ping outbound. Receiving inbound Pings from the attacker never sets `processing = true`, so the timeout path is never triggered for the attacker: [5](#0-4) 

**Why the first protection pass does not save the attacker:**

`ping_rtt` is only set in `pong_received()`, not in `ping_received()`. An attacker sending only Pings has `ping_rtt = None`, which maps to `u64::MAX` in the first sort — placing them at the *front* (worst RTT), so they are **not** protected by the first pass and remain in the candidate pool until the second pass protects them via the timestamp flood: [6](#0-5) 

---

### Impact Explanation

When the node reaches `max_inbound` capacity and a new legitimate peer attempts to connect, `accept_peer()` calls `try_evict_inbound_peer()`: [7](#0-6) 

The attacker's peer is always among the 8 most recently active (due to the Ping flood) and is removed from the eviction candidate set. The attacker's slot is never reclaimed. One inbound slot is permanently occupied, and the legitimate peer is rejected with `PeerError::ReachMaxInboundLimit` if no other unprotected candidate exists.

---

### Likelihood Explanation

The attack requires only a standard P2P connection and the ability to send valid `Ping` messages in a loop — both are trivially achievable by any node on the network. No cryptographic material, privileged access, or hashpower is needed. The `Ping` message format is public and simple (a 4-byte nonce): [8](#0-7) 

---

### Recommendation

1. **Separate the eviction-protection timestamp from unsolicited Pings.** Only update `last_ping_protocol_message_received_at` in `pong_received()` (a response to a node-initiated Ping), not in `ping_received()`. A Pong response is harder to fake because it must echo the correct nonce from a Ping the node sent.
2. **Rate-limit inbound Ping messages** per session to prevent flooding.
3. Alternatively, rename/split the field so that the eviction criterion tracks only *Pong* receipts (genuine round-trip activity), not inbound Pings.

---

### Proof of Concept

```
1. Node N has max_inbound = K (all slots filled with legitimate peers).
2. Attacker A connects as inbound peer, evicting one legitimate peer.
3. A enters a tight loop: send Ping(nonce=0) → node replies Pong → repeat.
   (Or just send Ping without waiting for Pong — no nonce validation on receipt.)
4. A's last_ping_protocol_message_received_at is refreshed to Instant::now() on every iteration.
5. A new legitimate peer L tries to connect → accept_peer() calls try_evict_inbound_peer().
6. Second sort_then_drop protects the 8 most recently active peers; A is always in that set.
7. A is never selected for eviction; L receives ReachMaxInboundLimit.
8. Invariant test: fill max_inbound slots, have all of them flood Pings,
   assert that try_evict_inbound_peer() returns None (no evictable candidate).
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

**File:** network/src/protocols/ping.rs (L291-305)
```rust
    fn build_ping(nonce: u32) -> Bytes {
        let nonce_le = nonce.to_le_bytes();
        let nonce = packed::Uint32::new_builder()
            .nth0(nonce_le[0])
            .nth1(nonce_le[1])
            .nth2(nonce_le[2])
            .nth3(nonce_le[3])
            .build();
        let ping = packed::Ping::new_builder().nonce(nonce).build();
        let payload = packed::PingPayload::new_builder().set(ping).build();
        packed::PingMessage::new_builder()
            .payload(payload)
            .build()
            .as_bytes()
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

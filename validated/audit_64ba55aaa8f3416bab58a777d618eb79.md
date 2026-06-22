### Title
Unconditional `last_ping_protocol_message_received_at` Update on Ping Receipt Enables Eviction-Protection Slot Squatting — (`network/src/protocols/ping.rs`)

---

### Summary

In `PingHandler::received`, `ping_received()` updates `last_ping_protocol_message_received_at` **before** `send_message` is called and **regardless of whether `send_message` succeeds**. Because there is no rate-limit on incoming Ping messages, an unprivileged inbound peer can send Pings at arbitrary frequency to keep its eviction-protection timestamp perpetually fresh, defeating the eviction algorithm and squatting an inbound slot indefinitely.

---

### Finding Description

The `received` handler processes an incoming `Ping` as follows:

```rust
PingPayload::Ping(nonce) => {
    self.ping_received(session.id);          // ← timestamp written here
    if context
        .send_message(PingMessage::build_pong(nonce))
        .await
        .is_err()
    {
        debug!("Failed to send message");    // ← error silently ignored
    }
}
``` [1](#0-0) 

`ping_received` writes `Instant::now()` into `peer.last_ping_protocol_message_received_at` unconditionally:

```rust
fn ping_received(&mut self, id: SessionId) {
    self.network_state.with_peer_registry_mut(|reg| {
        if let Some(peer) = reg.get_peer_mut(id) {
            peer.last_ping_protocol_message_received_at = Some(Instant::now());
        }
    });
}
``` [2](#0-1) 

The eviction algorithm in `try_evict_inbound_peer` explicitly protects the `EVICTION_PROTECT_PEERS = 8` inbound peers with the most recent `last_ping_protocol_message_received_at`, under the stated assumption that this characteristic is *"hard to simulate or manipulate"*: [3](#0-2) [4](#0-3) 

There is **no rate-limiting** on incoming Ping messages in the ping protocol. The `received` handler processes every arriving byte unconditionally.

---

### Impact Explanation

An attacker who connects as an inbound peer and sends Ping messages at a high rate will always have `last_ping_protocol_message_received_at ≈ Instant::now()`, guaranteeing placement in the top-8 "most recently active" protection bucket. The eviction algorithm will never select this peer for removal, even when the victim node is at `max_inbound` capacity and legitimate peers are being turned away.

The "send buffer full" sub-scenario (Pong dropped, `send_message` returns `Err`) makes this worse: the attacker keeps its timestamp fresh even when the victim **cannot actually communicate back**, violating the invariant that the field should reflect a live, bidirectional exchange. But the simpler scenario — no buffer pressure, Pong delivered — is equally exploitable because the timestamp is written before the send attempt. [5](#0-4) 

---

### Likelihood Explanation

- **Entry point**: standard P2P inbound connection, no privilege required.
- **Cost**: sending small Ping packets at a configurable rate; trivially cheap.
- **Preconditions**: none beyond connecting; the "send buffer full" variant requires a loaded node but the basic variant works unconditionally.
- **No existing guard**: no rate-limit, no nonce-validation on incoming Pings, no check that a Pong was successfully delivered before updating the timestamp.

---

### Recommendation

1. **Move the timestamp update to after a successful Pong send**, or better, **remove the timestamp update from the Ping branch entirely** — `last_ping_protocol_message_received_at` should only be refreshed in `pong_received`, which already does so after verifying the nonce and completing the round-trip.
2. **Add a per-session rate-limit** on incoming Ping messages (e.g., one per interval window) to prevent flooding.

---

### Proof of Concept

1. Connect to a victim node as an inbound peer.
2. In a tight loop, send `PingMessage::build_ping(nonce)` frames as fast as the TCP socket allows.
3. Observe that `peer.last_ping_protocol_message_received_at` is updated on every iteration (readable via debug state or a patched log).
4. Fill the victim's inbound slots with legitimate peers; trigger eviction by adding one more peer.
5. Assert that the attacker's session is **never** selected for eviction despite being the newest and least useful peer, because its timestamp is always `≈ now`.
6. For the send-buffer variant: throttle the victim's outbound socket (e.g., `tc qdisc`), repeat step 2, and confirm via debug logs that `send_message` returns `Err` while the timestamp continues to advance. [1](#0-0) [6](#0-5)

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

**File:** network/src/protocols/ping.rs (L215-223)
```rust
                    PingPayload::Ping(nonce) => {
                        self.ping_received(session.id);
                        if context
                            .send_message(PingMessage::build_pong(nonce))
                            .await
                            .is_err()
                        {
                            debug!("Failed to send message");
                        }
```

**File:** network/src/peer_registry.rs (L17-17)
```rust
pub(crate) const EVICTION_PROTECT_PEERS: usize = 8;
```

**File:** network/src/peer_registry.rs (L141-211)
```rust
    // try to evict an inbound peer
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

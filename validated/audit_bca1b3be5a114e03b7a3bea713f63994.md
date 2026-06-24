All four factual claims check out against the actual source code. Here is the validated report:

---

Audit Report

## Title
Unconditional `last_ping_protocol_message_received_at` Update on Ping Receipt Enables Eviction-Protection Slot Squatting — (`network/src/protocols/ping.rs`)

## Summary
`PingHandler::received` calls `ping_received()` unconditionally on every incoming Ping frame, writing `Instant::now()` into `peer.last_ping_protocol_message_received_at` before attempting to send a Pong and regardless of whether the send succeeds. Because there is no rate-limit on incoming Ping messages, an inbound peer can send Pings at arbitrary frequency to keep its eviction-protection timestamp perpetually fresh, defeating the eviction algorithm's "most recently active" protection bucket and squatting an inbound slot indefinitely.

## Finding Description
In `ping.rs` lines 215–223, `ping_received(session.id)` is called first at line 216, then `send_message` is attempted at line 218; a send failure is silently logged and does not roll back the timestamp. [1](#0-0) 

`ping_received` (lines 62–69) writes `Instant::now()` into `peer.last_ping_protocol_message_received_at` with no rate check or frequency guard. [2](#0-1) 

In `peer_registry.rs` lines 167–183, `try_evict_inbound_peer` protects the 8 inbound peers (`EVICTION_PROTECT_PEERS = 8`) with the smallest `now.saturating_duration_since(last_ping_protocol_message_received_at)` value — i.e., the 8 most recently "active" peers. The `sort_then_drop` helper sorts descending by duration and truncates the oldest, retaining the most-recent 8 as protected. [3](#0-2) 

The comment at line 149 asserts this characteristic is *"hard to simulate or manipulate"*; the absence of any rate-limit makes that assumption false. [4](#0-3) 

An attacker who sends Ping frames in a tight loop will always have a timestamp of `≈ Instant::now()`, guaranteeing placement in this protection bucket. The `received` handler processes every incoming Ping unconditionally with no per-session counter or interval check. [5](#0-4) 

## Impact Explanation
An attacker holding one inbound slot can prevent that slot from ever being reclaimed by the eviction algorithm. With multiple connections from distinct network groups (to survive the network-group grouping step at lines 191–203), an attacker can fill all inbound slots and block legitimate peers from connecting. This matches the allowed impact: **"Vulnerabilities or bad designs which could cause CKB network congestion with few costs"** (High, 10001–15000 points). [6](#0-5) 

## Likelihood Explanation
- Entry point: standard inbound P2P connection, no privilege required.
- Cost: sending small Ping frames at a configurable rate; trivially cheap.
- Preconditions: none beyond connecting.
- No existing guard: no rate-limit, no per-session Ping counter, no check that a Pong was successfully delivered before updating the timestamp.
- Repeatable and stable: the attacker's timestamp is always `≈ now` as long as the loop runs.

## Recommendation
1. **Move the timestamp update out of the Ping branch entirely.** `last_ping_protocol_message_received_at` should only be refreshed in `pong_received` (lines 71–79), which already does so after verifying the nonce and completing the round-trip. This restores the invariant that the field reflects a live, bidirectional exchange. [7](#0-6) 

2. **Add a per-session rate-limit** on incoming Ping messages (e.g., one per interval window matching `self.interval`) to prevent flooding regardless of the timestamp logic.

## Proof of Concept
1. Connect to a victim node as an inbound peer.
2. In a tight loop, send `PingMessage::build_ping(nonce)` frames as fast as the TCP socket allows.
3. Confirm via debug logs or a patched assertion that `peer.last_ping_protocol_message_received_at` is updated on every iteration.
4. Fill the victim's remaining inbound slots with legitimate peers; trigger eviction by adding one more peer.
5. Assert that the attacker's session is never selected for eviction: its `last_ping_protocol_message_received_at` is always `≈ now`, placing it in the top-8 protection bucket at lines 168–183.
6. For the send-buffer variant: throttle the victim's outbound socket (e.g., `tc qdisc add dev lo root netem delay 10000ms`), repeat step 2, and confirm via debug logs that `send_message` returns `Err` while the timestamp continues to advance.

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

**File:** network/src/protocols/ping.rs (L201-249)
```rust
    async fn received(&mut self, context: ProtocolContextMutRef<'_>, data: Bytes) {
        let session = context.session;
        match PingMessage::decode(data.as_ref()) {
            None => {
                error!("Message decode error");
                if let Err(err) =
                    async_disconnect_with_message(context.control(), session.id, "ping failed")
                        .await
                {
                    debug!("Disconnect failed {:?}, error: {:?}", session.id, err);
                }
            }
            Some(msg) => {
                match msg {
                    PingPayload::Ping(nonce) => {
                        self.ping_received(session.id);
                        if context
                            .send_message(PingMessage::build_pong(nonce))
                            .await
                            .is_err()
                        {
                            debug!("Failed to send message");
                        }
                    }
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
                        // if nonce is incorrect or can't find ping info
                        if let Err(err) = async_disconnect_with_message(
                            context.control(),
                            session.id,
                            "ping failed",
                        )
                        .await
                        {
                            debug!("Disconnect failed {:?}, error: {:?}", session.id, err);
                        }
                    }
                }
            }
        }
    }
```

**File:** network/src/peer_registry.rs (L149-149)
```rust
        // Protect peers based on characteristics that an attacker hard to simulate or manipulate
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

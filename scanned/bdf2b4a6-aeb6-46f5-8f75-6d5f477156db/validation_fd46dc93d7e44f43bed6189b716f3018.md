### Title
Zero-Cost Inbound Peer Eviction Loop via Self-Disconnect on `/ckb/disconnectmsg` — (`network/src/protocols/disconnect_message.rs` + `network/src/peer_registry.rs`)

---

### Summary

An unprivileged remote peer can repeatedly trigger the inbound eviction algorithm at negligible cost by connecting (forcing eviction of a legitimate peer), then immediately sending a message on the `/ckb/disconnectmsg` protocol to cause the node to disconnect the attacker's own session — freeing the slot with zero penalty — and repeating indefinitely.

---

### Finding Description

The attack exploits two independent, correct-looking behaviors that compose into a vulnerability:

**Step 1 — Eviction on connect.**
`PeerRegistry::accept_peer` is called from `EventHandler::handle_event` on every `SessionOpen`. When `non_whitelist_inbound >= max_inbound`, it calls `try_evict_inbound_peer`, removes the chosen victim, and then inserts the new (attacker) peer. [1](#0-0) 

Critically, the attacker peer is not yet in `self.peers` when `try_evict_inbound_peer` runs (insertion happens at line 137), so the attacker can never be selected as the eviction target. [2](#0-1) 

**Step 2 — Zero-cost self-disconnect.**
`DisconnectMessageProtocol::received` logs the message and calls `context.disconnect(session_id)` on the sender's own session. There is no ban, no score penalty, no rate limiting, and no flag set on the peer record. [3](#0-2) 

The comment at line 33 even acknowledges that punishment was considered but never implemented: `// Maybe punish this peer later`.

**Step 3 — Slot freed with no consequence.**
When the attacker's session closes, `SessionClose` fires, `remove_peer` removes the attacker from the registry, and `remove_disconnected_peer` is called on the peer store. No ban entry is written. [4](#0-3) 

The attacker's slot is now free. The attacker reconnects (with the same or a new peer ID — both work, since the registry entry was removed) and the cycle repeats.

---

### Impact Explanation

Each cycle costs the attacker one TCP handshake and one protocol message. Each cycle evicts one legitimate inbound peer. With `max_inbound=125` (default), a single attacker running this loop continuously can churn the entire inbound peer set, preventing honest peers from maintaining stable connections to the target node. This degrades block/transaction propagation and can isolate the target node from the honest network, causing network congestion and connectivity disruption at O(1) attacker cost per eviction.

The eviction algorithm does protect peers with low ping RTT, recent ping messages, and long connection time, but newly-connected honest peers (no ping data yet, short connection time) are fully unprotected and will be preferentially evicted first.

---

### Likelihood Explanation

The path is fully reachable from the public P2P interface with no privileges. The attacker needs only the ability to open TCP connections to the target's listen address, which is public by design. No PoW, no keys, no Sybil majority required. The attack is repeatable at network speed. No existing guard in the reviewed code (ban list, score system, rate limiter) is triggered by self-disconnection via the disconnect message protocol.

---

### Recommendation

1. **Ban or rate-limit on disconnect-message receipt.** In `DisconnectMessageProtocol::received`, before calling `context.disconnect`, apply a short-duration ban (e.g., 60–300 seconds) to the sender's address via `network_state.ban_session(...)`, consistent with how `ProtocolError` is handled. [5](#0-4) 

2. **Track rapid connect/disconnect cycles.** Add a per-IP or per-peer-ID connection-rate counter in `PeerStore` or `PeerRegistry`. Reject or ban peers that connect and disconnect faster than a threshold (e.g., more than N times per minute).

3. **Require minimum session age before eviction counts.** In `try_evict_inbound_peer`, exclude peers whose `connected_time` is below a minimum threshold (e.g., 30 seconds) from the candidate set, so a brand-new attacker peer cannot immediately trigger eviction of a long-lived peer.

---

### Proof of Concept

```
Setup: target node with max_inbound=3
1. Connect honest peers A, B, C → slots full (non_whitelist_inbound=3)
2. Attacker connects → accept_peer calls try_evict_inbound_peer
   → one of A/B/C is evicted (e.g., C)
   → attacker inserted into registry (slot 3)
3. Attacker sends any bytes on /ckb/disconnectmsg
   → DisconnectMessageProtocol::received fires
   → context.disconnect(attacker_session) called
   → no ban written
4. SessionClose fires → attacker removed from registry (slot freed)
5. Repeat from step 2 → B evicted next cycle, then A
6. After 10 cycles: A, B, C have each been evicted multiple times;
   attacker has paid only 10 TCP connections + 10 messages
```

The invariant violated: **inbound eviction must not be triggerable at zero cost by a peer that immediately self-disconnects.**

### Citations

**File:** network/src/peer_registry.rs (L115-138)
```rust
            if session_type.is_inbound() {
                if connection_status.non_whitelist_inbound >= self.max_inbound {
                    if let Some(evicted_session) = self.try_evict_inbound_peer(peer_store) {
                        evicted_peer = self.remove_peer(evicted_session);
                    } else {
                        return Err(PeerError::ReachMaxInboundLimit.into());
                    }
                }
            } else if connection_status.non_whitelist_outbound >= self.max_outbound {
                if self.disable_block_relay_only_connection
                    || connection_status.block_relay_only_outbound_count
                        >= self.max_outbound_block_relay
                {
                    return Err(PeerError::ReachMaxOutboundLimit.into());
                } else {
                    peer_store.add_anchors(remote_addr.clone());
                    session_type = SessionType::BlockRelayOnly;
                }
            }
        }
        peer_store.add_connected_peer(remote_addr.clone(), session_type);
        let peer = Peer::new(session_id, session_type, remote_addr, is_whitelist);
        self.peers.insert(session_id, peer);
        Ok(evicted_peer)
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

**File:** network/src/protocols/disconnect_message.rs (L25-42)
```rust
    async fn received(&mut self, context: ProtocolContextMutRef<'_>, data: Bytes) {
        let session_id = context.session.id;
        if let Ok(message) = String::from_utf8(data.to_vec()) {
            info!(
                "Received disconnect message from peer={}: {}",
                session_id, message
            );
        } else {
            // Maybe punish this peer later (also when send us too large message)
            debug!(
                "[WARNING]: peer {} send us a malformed disconnect message!",
                session_id
            );
        }
        if let Err(err) = context.disconnect(session_id).await {
            debug!("Disconnect {:?} failed, error: {:?}", session_id, err);
        }
    }
```

**File:** network/src/network.rs (L650-656)
```rust
                // Ban because misbehave of remote peer
                self.network_state.ban_session(
                    &context.control().clone().into(),
                    id,
                    Duration::from_secs(300),
                    message,
                );
```

**File:** network/src/network.rs (L795-813)
```rust
            ServiceEvent::SessionClose { session_context } => {
                debug!(
                    "SessionClose({}, {})",
                    session_context.id, session_context.address,
                );
                let peer_exists = self.network_state.with_peer_registry_mut(|reg| {
                    // should make sure feelers is clean
                    reg.remove_feeler(&session_context.address);
                    reg.remove_peer(session_context.id).is_some()
                });
                if peer_exists {
                    debug!(
                        "{} closed. Remove {} from peer_registry",
                        session_context.id, session_context.address,
                    );
                    self.network_state.with_peer_store_mut(|peer_store| {
                        peer_store.remove_disconnected_peer(&session_context.address);
                    });
                }
```

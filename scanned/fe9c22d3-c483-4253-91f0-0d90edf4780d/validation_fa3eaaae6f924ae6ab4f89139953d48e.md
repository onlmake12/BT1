Based on my analysis of the actual code, here is my determination:

---

### Title
Missing Peer Ban for Malformed Disconnect Messages Enables Inbound Slot Exhaustion via Reconnect Loop — (`network/src/protocols/disconnect_message.rs`)

### Summary

The `DisconnectMessageProtocol::received` handler explicitly skips penalizing peers that send non-UTF-8 binary payloads, as noted by the `// Maybe punish this peer later` comment. Because no ban is issued, an attacker can connect, send a malformed payload, get disconnected, and immediately reconnect — indefinitely — without ever being blocked. When inbound slots are full, each reconnection triggers the eviction logic against honest peers.

### Finding Description

In `network/src/protocols/disconnect_message.rs`, the `received` function handles the `/ckb/disconnectmsg` protocol: [1](#0-0) 

When `String::from_utf8` fails, the code logs a debug message and falls through to `context.disconnect(session_id)`. No call to `ban_session` or `ban_addr` is made. The comment on line 33 explicitly acknowledges this as unimplemented.

The ban infrastructure is fully present elsewhere. `NetworkState::ban_session` in `network/src/network.rs` calls `peer_store.ban_addr(...)`, and `accept_peer` in `peer_registry.rs` checks `peer_store.is_addr_banned(&remote_addr)` before admitting a new peer: [2](#0-1) 

Because the attacker is never banned, this check always passes on reconnect.

### Impact Explanation

When inbound slots are full (`non_whitelist_inbound >= max_inbound`), `accept_peer` calls `try_evict_inbound_peer` to make room: [3](#0-2) 

The eviction logic selects from **existing** peers (not the incoming attacker), protecting only up to `EVICTION_PROTECT_PEERS` (8) by ping RTT, 8 by recent messages, and half of the remainder by connection time: [4](#0-3) 

Unprotected honest peers are evicted to make room for the attacker. Since the attacker is never banned, this cycle repeats without bound, continuously displacing legitimate peers from inbound slots.

### Likelihood Explanation

The attack requires only a TCP connection and the ability to send arbitrary bytes — no special privileges, no leaked keys, no hashpower. The attacker must use a fresh peer ID (Ed25519 keypair generation is microsecond-scale) on each reconnect to avoid the `PeerIdExists` check, but this is trivially automated. The secio handshake adds latency but does not prevent the attack at any realistic rate.

### Recommendation

In the `else` branch of `received` (line 32–38 of `disconnect_message.rs`), call `self.0.ban_session(...)` (or equivalent) with a short ban duration (e.g., 300 seconds) before disconnecting. The `ban_session` method already exists in `NetworkState`: [5](#0-4) 

This mirrors the pattern used for `ProtocolError` in `handle_error`: [6](#0-5) 

### Proof of Concept

1. Start a CKB node with `max_inbound = N`.
2. Fill `N` inbound slots with honest peers.
3. From an attacker process: connect, open protocol `/ckb/disconnectmsg`, send `[0xFF, 0xFE]` (invalid UTF-8), observe disconnect with no ban entry in `get_banned_addrs`.
4. Assert `ban_list.count() == 0` after the malformed message.
5. Immediately reconnect with a new keypair; observe that an honest peer is evicted (`try_evict_inbound_peer` runs).
6. Repeat step 3–5 in a tight loop; confirm honest peers are continuously displaced while the attacker is never blocked.

### Citations

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

**File:** network/src/peer_registry.rs (L109-111)
```rust
            if peer_store.is_addr_banned(&remote_addr) {
                return Err(PeerError::Banned.into());
            }
```

**File:** network/src/peer_registry.rs (L115-122)
```rust
            if session_type.is_inbound() {
                if connection_status.non_whitelist_inbound >= self.max_inbound {
                    if let Some(evicted_session) = self.try_evict_inbound_peer(peer_store) {
                        evicted_peer = self.remove_peer(evicted_session);
                    } else {
                        return Err(PeerError::ReachMaxInboundLimit.into());
                    }
                }
```

**File:** network/src/peer_registry.rs (L142-210)
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
```

**File:** network/src/network.rs (L241-281)
```rust
    pub(crate) fn ban_session(
        &self,
        p2p_control: &ServiceControl,
        session_id: SessionId,
        duration: Duration,
        reason: String,
    ) {
        if let Some(addr) = self.with_peer_registry(|reg| {
            reg.get_peer(session_id)
                .filter(|peer| !peer.is_whitelist)
                .map(|peer| peer.connected_addr.clone())
        }) {
            info!(
                "Ban peer {:?} for {} seconds, reason: {}",
                addr,
                duration.as_secs(),
                reason
            );
            if let Some(metrics) = ckb_metrics::handle() {
                metrics.ckb_network_ban_peer.inc();
            }
            if let Some(peer) = self.with_peer_registry_mut(|reg| reg.remove_peer(session_id)) {
                let message = format!("Ban for {} seconds, reason: {}", duration.as_secs(), reason);
                self.peer_store.lock().ban_addr(
                    &peer.connected_addr,
                    duration.as_millis() as u64,
                    reason,
                );
                if let Err(err) =
                    disconnect_with_message(p2p_control, peer.session_id, message.as_str())
                {
                    debug!("Disconnect failed {:?}, error: {:?}", peer.session_id, err);
                }
            }
        } else {
            debug!(
                "Ban session({}) failed: not found in peer registry or it is on the whitelist",
                session_id
            );
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

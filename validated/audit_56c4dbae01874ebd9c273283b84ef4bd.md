### Title
Missing Ban Penalty in `check_protocol_type` Enables Free Reconnect Cycling for Inbound Slot Exhaustion — (`network/src/services/protocol_type_checker.rs`)

### Summary

`check_protocol_type` disconnects peers that open zero protocols using only `disconnect_with_message`, without calling `ban_session` or `report_session`. Because no ban is recorded, the peer's address passes `is_addr_banned` on the very next connection attempt, allowing the attacker to cycle each inbound slot every ~40 seconds indefinitely.

### Finding Description

`check_protocol_type` iterates all registered peers and, for any peer whose connected time exceeds `TIMEOUT` (10 s) and whose open protocol set is neither `FullyOpen` nor `Feeler`, calls only `disconnect_with_message`: [1](#0-0) 

No call to `ban_session` or `report_session` is made anywhere in this function. Compare with other misbehaviour handlers (e.g. `ProtocolError`, `ProtocolHandleError`) that explicitly call `ban_session` with a 300-second duration: [2](#0-1) 

On `SessionClose`, the node calls `peer_store.remove_disconnected_peer`, which only removes the peer from the `connected_peers` map — no score penalty, no ban entry: [3](#0-2) 

`accept_peer` only checks `peer_store.is_addr_banned` before admitting a new connection: [4](#0-3) 

Since no ban was written, the attacker's address passes this check immediately and a new slot is consumed.

The cycle time is bounded by `TIMEOUT = 10 s` (grace period before the check fires) plus up to `CHECK_INTERVAL = 30 s` (periodic poll interval): [5](#0-4) 

### Impact Explanation

With `max_inbound = N` slots, an attacker operating N TCP connections that open zero protocols can keep all slots occupied. When an honest peer connects and triggers `try_evict_inbound_peer`, the eviction algorithm protects peers by lowest ping, most-recent message, and longest connection time: [6](#0-5) 

Attacker connections have `ping_rtt = None` (→ `u64::MAX`, worst) and `last_ping_protocol_message_received_at = None` (→ `u64::MAX`, worst), so they are not protected by the first two criteria. However, the third criterion — "protect half of remaining candidates with the longest connection time" — does protect staggered attacker connections that have been present for longer than the newly admitted honest peer. A newly connected honest peer (also no ping, no messages, short connection time) is therefore equally or more likely to be evicted in the next eviction cycle than the attacker's older connections. The attacker reconnects immediately (no ban), restoring the full N-slot occupation.

### Likelihood Explanation

The attack requires only the ability to open TCP connections to the node's P2P port — no authentication, no PoW, no special capability. The attacker needs N concurrent connections (one per inbound slot). The default `max_inbound` is 125. Each connection consumes negligible bandwidth (no protocol messages are sent). The cycle is fully automatable and repeatable without limit.

### Recommendation

In `check_protocol_type`, replace the bare `disconnect_with_message` call with a call to `network_state.ban_session` (or at minimum `report_session` with a sufficiently negative behaviour score) so that the peer's IP is entered into the ban list for a meaningful duration (e.g. 300 seconds, consistent with other misbehaviour handlers). This prevents immediate reconnection after protocol-compliance eviction.

### Proof of Concept

State-machine test:
1. Configure a node with `max_inbound = N` (e.g. 5 for testing).
2. Spawn N attacker TCP connections that complete the P2P handshake but open zero sub-protocols.
3. Wait `TIMEOUT + CHECK_INTERVAL` (40 s). Observe all N connections are disconnected via `disconnect_with_message`.
4. Immediately reconnect all N attacker connections. Observe all N slots are re-accepted (no ban check fails).
5. Attempt to connect an honest peer (opens all required protocols). Assert it is rejected (`ReachMaxInboundLimit`) or immediately evicted.
6. Repeat steps 3–5 for multiple cycles. Assert the honest peer's acceptance rate is near zero while attacker reconnects freely. [7](#0-6) [8](#0-7)

### Citations

**File:** network/src/services/protocol_type_checker.rs (L23-24)
```rust
const TIMEOUT: Duration = Duration::from_secs(10);
const CHECK_INTERVAL: Duration = Duration::from_secs(30);
```

**File:** network/src/services/protocol_type_checker.rs (L81-106)
```rust
    pub(crate) fn check_protocol_type(&self) {
        self.network_state.with_peer_registry(|reg| {
            let now = Instant::now();
            for (session_id, peer) in reg.peers() {
                // skip just connected peers
                if now.saturating_duration_since(peer.connected_time) < TIMEOUT {
                    continue;
                }

                // check open protocol type
                if let Err(err) = self.opened_protocol_type(peer) {
                    debug!(
                        "Close peer {:?} due to open protocols error: {}",
                        peer.connected_addr, err
                    );
                    if let Err(err) = disconnect_with_message(
                        &self.p2p_control,
                        *session_id,
                        &format!("open protocols error: {err}"),
                    ) {
                        debug!("Disconnect failed {session_id:?}, error: {err:?}");
                    }
                }
            }
        });
    }
```

**File:** network/src/network.rs (L643-656)
```rust
            ServiceError::ProtocolError {
                id,
                proto_id,
                error,
            } => {
                debug!("ProtocolError({}, {}) {}", id, proto_id, error);
                let message = format!("ProtocolError id={proto_id}");
                // Ban because misbehave of remote peer
                self.network_state.ban_session(
                    &context.control().clone().into(),
                    id,
                    Duration::from_secs(300),
                    message,
                );
```

**File:** network/src/peer_store/peer_store_impl.rs (L169-172)
```rust
    /// Remove peer id
    pub fn remove_disconnected_peer(&mut self, addr: &Multiaddr) -> Option<PeerInfo> {
        extract_peer_id(addr).and_then(|peer_id| self.connected_peers.remove(&peer_id))
    }
```

**File:** network/src/peer_registry.rs (L86-139)
```rust
    pub(crate) fn accept_peer(
        &mut self,
        remote_addr: Multiaddr,
        session_id: SessionId,
        raw_session_type: RawSessionType,
        peer_store: &mut PeerStore,
    ) -> Result<Option<Peer>, Error> {
        if self.peers.contains_key(&session_id) {
            return Err(PeerError::SessionExists(session_id).into());
        }
        let peer_id = extract_peer_id(&remote_addr).expect("opened session should have peer id");
        if self.get_key_by_peer_id(&peer_id).is_some() {
            return Err(PeerError::PeerIdExists(peer_id).into());
        }

        let is_whitelist = self.whitelist_peers.contains(&peer_id);
        let mut evicted_peer: Option<Peer> = None;

        let mut session_type: SessionType = raw_session_type.into();
        if !is_whitelist {
            if self.whitelist_only {
                return Err(PeerError::NonReserved.into());
            }
            if peer_store.is_addr_banned(&remote_addr) {
                return Err(PeerError::Banned.into());
            }

            let connection_status = self.connection_status();
            // check peers connection limitation
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
    }
```

**File:** network/src/peer_registry.rs (L149-188)
```rust
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
```

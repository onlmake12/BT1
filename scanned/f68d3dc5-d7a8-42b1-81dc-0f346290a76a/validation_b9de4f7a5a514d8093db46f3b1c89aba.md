Audit Report

## Title
Missing Peer Ban for Malformed Disconnect Messages Enables Inbound Slot Exhaustion via Reconnect Loop — (File: `network/src/protocols/disconnect_message.rs`)

## Summary

In `DisconnectMessageProtocol::received`, when a peer sends a non-UTF-8 payload on the `/ckb/disconnectmsg` protocol, the handler logs a debug message and disconnects the peer but issues no ban. Because the attacker's address is never added to the ban list, the `is_addr_banned` check in `accept_peer` always passes on reconnect. An attacker can loop: connect → send malformed bytes → disconnect (no ban) → reconnect with a fresh keypair → trigger eviction of an honest peer → repeat, continuously displacing legitimate inbound peers at negligible cost.

## Finding Description

In `received` (lines 25–42 of `disconnect_message.rs`), the `else` branch for a failed `String::from_utf8` only logs and falls through to `context.disconnect`: [1](#0-0) 

No call to `self.0.ban_session(...)` is made. The comment on line 33 explicitly acknowledges this as unimplemented.

When the attacker reconnects (with a fresh Ed25519 keypair to bypass the `PeerIdExists` check), `accept_peer` in `peer_registry.rs` checks: [2](#0-1) 

Because no ban was recorded, this check passes. If inbound slots are full (`non_whitelist_inbound >= max_inbound`), `try_evict_inbound_peer` is called: [3](#0-2) 

The eviction logic selects from **existing** honest peers — not the incoming attacker — protecting only up to `EVICTION_PROTECT_PEERS` (8) by ping RTT, 8 by recent messages, and half the remainder by connection time: [4](#0-3) 

The ban infrastructure (`ban_session` → `peer_store.ban_addr`) is fully present and used elsewhere (e.g., `ProtocolError` handling at lines 650–656 of `network.rs`): [5](#0-4) [6](#0-5) 

It is simply not called in the malformed-disconnect path.

## Impact Explanation

An unprivileged attacker with TCP access can continuously evict honest inbound peers from a target node's inbound slot table. With enough repetition, the target node's inbound connections are entirely populated by attacker-controlled or transient connections, effectively isolating it from the honest network. This matches the allowed impact: **"Vulnerabilities or bad designs which could cause CKB network congestion with few costs" — High (10001–15000 points)**.

## Likelihood Explanation

The attack requires only: (1) TCP connectivity to the target node, (2) completing the secio handshake (adds latency, not a barrier), and (3) generating a fresh Ed25519 keypair per reconnect (microsecond-scale). No special privileges, leaked keys, or hashpower are needed. The loop is trivially automatable. The `PeerIdExists` check is bypassed by using a new keypair each iteration. The attack is repeatable without bound since no ban is ever issued.

## Recommendation

In the `else` branch of `received` (lines 32–38 of `disconnect_message.rs`), call `self.0.ban_session(p2p_control, session_id, Duration::from_secs(300), "malformed disconnect message".to_string())` before or instead of `context.disconnect`. This mirrors the existing pattern used for `ProtocolError` in `handle_error` (lines 650–656 of `network.rs`). The `p2p_control` handle can be obtained from `context.control().clone().into()`, consistent with other protocol handlers that already hold an `Arc<NetworkState>`.

## Proof of Concept

1. Start a CKB node with `max_inbound = N`.
2. Fill all `N` inbound slots with honest peers (e.g., via `N` controlled connections that stay open).
3. From an attacker process: connect to the target, complete the secio handshake with a fresh keypair, open protocol `/ckb/disconnectmsg`, send `[0xFF, 0xFE]` (invalid UTF-8).
4. Observe the target disconnects the attacker session; call `get_banned_addrs` RPC and assert the attacker's address is **not** present.
5. Immediately reconnect with a new keypair; observe via logs that `try_evict_inbound_peer` ran and an honest peer was disconnected with "evict because accepted better peer".
6. Repeat steps 3–5 in a tight loop; confirm via `get_peers` RPC that honest peers are continuously displaced while the attacker is never blocked.

### Citations

**File:** network/src/protocols/disconnect_message.rs (L32-38)
```rust
        } else {
            // Maybe punish this peer later (also when send us too large message)
            debug!(
                "[WARNING]: peer {} send us a malformed disconnect message!",
                session_id
            );
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

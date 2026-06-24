Looking at the actual code to validate each claim in the report.

Audit Report

## Title
Missing Peer Ban for Malformed Disconnect Messages Enables Inbound Slot Exhaustion via Reconnect Loop — (File: `network/src/protocols/disconnect_message.rs`)

## Summary
In `DisconnectMessageProtocol::received`, when a peer sends a non-UTF-8 payload on the `/ckb/disconnectmsg` protocol, the handler logs a debug message and disconnects the peer but issues no ban. Because the attacker's address is never added to the ban list, the `is_addr_banned` check in `accept_peer` always passes on reconnect. An attacker can loop: connect → trigger eviction of an honest peer → send malformed bytes → disconnect (no ban) → reconnect with a fresh keypair → repeat, continuously displacing legitimate inbound peers at negligible cost.

## Finding Description
In `received` (lines 32–38 of `disconnect_message.rs`), the `else` branch for a failed `String::from_utf8` only logs and falls through to `context.disconnect`:

```rust
} else {
    // Maybe punish this peer later (also when send us too large message)
    debug!(
        "[WARNING]: peer {} send us a malformed disconnect message!",
        session_id
    );
}
``` [1](#0-0) 

No call to `ban_session` is made. The comment on line 33 explicitly acknowledges this as unimplemented. The disconnect at line 39 fires unconditionally for both the valid and malformed branches, so the attacker's session is cleanly closed without any address-level penalty. [2](#0-1) 

When the attacker reconnects with a fresh Ed25519 keypair (new peer ID), `accept_peer` in `peer_registry.rs` first checks `PeerIdExists` (line 97–99) — bypassed by the new keypair — then checks `is_addr_banned` (lines 109–111): [3](#0-2) 

Because no ban was recorded, this check passes. If inbound slots are full (`non_whitelist_inbound >= max_inbound`), `try_evict_inbound_peer` is called (lines 115–121), selecting a victim from **existing honest peers**: [4](#0-3) 

The eviction logic at lines 142–211 protects only up to `EVICTION_PROTECT_PEERS` (8) by ping RTT, 8 by recent messages, and half the remainder by connection time — all drawn from the existing honest peer pool, never from the incoming attacker. [5](#0-4) 

The ban infrastructure is fully present and used elsewhere: `ban_session` (lines 241–281 of `network.rs`) is called for `ProtocolError` at lines 650–656: [6](#0-5) 

It is simply not called in the malformed-disconnect path.

## Impact Explanation
An unprivileged attacker with TCP access can continuously evict honest inbound peers from a target node's inbound slot table. With enough repetition, the target node's inbound connections are entirely populated by attacker-controlled or transient connections, effectively isolating it from the honest network. This matches the allowed impact: **"Vulnerabilities or bad designs which could cause CKB network congestion with few costs" — High (10001–15000 points)**. Targeting multiple nodes simultaneously scales the disruption to network-wide congestion.

## Likelihood Explanation
The attack requires only: (1) TCP connectivity to the target node, (2) completing the secio handshake (adds latency but is automatable), and (3) generating a fresh Ed25519 keypair per reconnect (microsecond-scale). No special privileges, leaked keys, or hashpower are needed. The loop is trivially automatable. The `PeerIdExists` check is bypassed by using a new keypair each iteration. The attack is repeatable without bound since no ban is ever issued, and the comment in the source code ("Maybe punish this peer later") confirms the gap is known but unaddressed.

## Recommendation
In the `else` branch of `received` (lines 32–38 of `disconnect_message.rs`), call `self.0.ban_session(p2p_control, session_id, Duration::from_secs(300), "malformed disconnect message".to_string())` before or instead of `context.disconnect`. The `p2p_control` handle can be obtained from `context.control().clone().into()`, consistent with the pattern already used in `handle_error` for `ProtocolError` (lines 650–656 of `network.rs`) and in `hole_punching/mod.rs` and `identify/mod.rs` which already call `ban_session` from protocol handlers holding an `Arc<NetworkState>`.

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

**File:** network/src/protocols/disconnect_message.rs (L39-41)
```rust
        if let Err(err) = context.disconnect(session_id).await {
            debug!("Disconnect {:?} failed, error: {:?}", session_id, err);
        }
```

**File:** network/src/peer_registry.rs (L97-111)
```rust
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

**File:** network/src/peer_registry.rs (L142-148)
```rust
    fn try_evict_inbound_peer(&self, _peer_store: &PeerStore) -> Option<SessionId> {
        let mut candidate_peers = {
            self.peers
                .values()
                .filter(|peer| peer.is_inbound() && !peer.is_whitelist)
                .collect::<Vec<_>>()
        };
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

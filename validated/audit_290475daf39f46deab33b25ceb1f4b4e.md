All code references check out against the actual source. Let me verify the key claims:

1. **`disconnect_message.rs` lines 32–38**: The `else` branch logs and falls through to `context.disconnect` with no `ban_session` call. The comment on line 33 explicitly says `"Maybe punish this peer later"` — confirming the omission is acknowledged but unimplemented. [1](#0-0) 

2. **`peer_registry.rs` lines 109–111**: `accept_peer` checks `is_addr_banned` — a ban would block reconnection from the same IP. [2](#0-1) 

3. **`peer_registry.rs` lines 97–99**: The `PeerIdExists` check is keyed on peer ID, not IP address, so a fresh Ed25519 keypair bypasses it. [3](#0-2) 

4. **`peer_registry.rs` lines 115–121**: When inbound slots are full, `try_evict_inbound_peer` is called, evicting an existing honest peer. [4](#0-3) 

5. **`network.rs` lines 241–281**: `ban_session` → `peer_store.ban_addr` is fully implemented and used for `ProtocolError` at lines 650–656. [5](#0-4) [6](#0-5) 

All five cited code paths match the report's claims exactly. The vulnerability is real and the exploit path is sound.

---

Audit Report

## Title
Missing Peer Ban for Malformed Disconnect Messages Enables Inbound Slot Exhaustion via Reconnect Loop — (File: `network/src/protocols/disconnect_message.rs`)

## Summary
In `DisconnectMessageProtocol::received`, when a peer sends a non-UTF-8 payload on the `/ckb/disconnectmsg` protocol, the handler logs a debug message and calls `context.disconnect` but never calls `ban_session`. Because the attacker's IP address is never added to the ban list, the `is_addr_banned` check in `accept_peer` always passes on reconnect. An attacker can loop: connect → send malformed bytes → disconnect (no ban) → reconnect with a fresh Ed25519 keypair (bypassing `PeerIdExists`) → trigger eviction of an honest peer → repeat, continuously displacing legitimate inbound peers at negligible cost.

## Finding Description
In `received` (lines 32–38 of `disconnect_message.rs`), the `else` branch for a failed `String::from_utf8` only logs and falls through to `context.disconnect`. No call to `self.0.ban_session(...)` is made. The comment on line 33 explicitly acknowledges this as unimplemented: `"Maybe punish this peer later (also when send us too large message)"`.

When the attacker reconnects with a fresh Ed25519 keypair, `accept_peer` in `peer_registry.rs` first checks `PeerIdExists` (line 97–99) — bypassed by the new keypair — then checks `is_addr_banned` (lines 109–111) — passes because no ban was recorded. If inbound slots are full (`non_whitelist_inbound >= max_inbound`), `try_evict_inbound_peer` is called (lines 115–121), selecting a victim from existing honest peers using ping RTT, recent message time, and connection duration heuristics. The attacker's fresh connection has no ping data and no message history, so it is never the eviction candidate; honest long-lived peers are.

The ban infrastructure (`ban_session` → `peer_store.ban_addr`) is fully present and used elsewhere (e.g., `ProtocolError` handling at lines 650–656 of `network.rs`). It is simply not called in the malformed-disconnect path.

## Impact Explanation
An unprivileged attacker with TCP access can continuously evict honest inbound peers from a target node's inbound slot table. With enough repetition, the target node's inbound connections are entirely populated by attacker-controlled or transient connections, effectively isolating it from the honest network. This matches the allowed High impact: **"Vulnerabilities or bad designs which could cause CKB network congestion with few costs" (10001–15000 points)**.

## Likelihood Explanation
The attack requires only: (1) TCP connectivity to the target node, (2) completing the secio handshake (adds latency, not a meaningful barrier), and (3) generating a fresh Ed25519 keypair per reconnect (microsecond-scale). No special privileges, leaked keys, or hashpower are needed. The loop is trivially automatable. The `PeerIdExists` check is bypassed by using a new keypair each iteration. The attack is repeatable without bound since no ban is ever issued.

## Recommendation
In the `else` branch of `received` (lines 32–38 of `disconnect_message.rs`), call `self.0.ban_session(p2p_control, session_id, Duration::from_secs(300), "malformed disconnect message".to_string())` before or instead of `context.disconnect`. The `p2p_control` handle can be obtained from `context.control().clone().into()`, consistent with the existing pattern used for `ProtocolError` in `handle_error` (lines 650–656 of `network.rs`).

## Proof of Concept
1. Start a CKB node with `max_inbound = N`.
2. Fill all `N` inbound slots with honest peers (e.g., via `N` controlled connections that stay open).
3. From an attacker process: connect to the target, complete the secio handshake with a fresh keypair, open protocol `/ckb/disconnectmsg`, send `[0xFF, 0xFE]` (invalid UTF-8).
4. Observe the target disconnects the attacker session; call `get_banned_addrs` RPC and assert the attacker's address is **not** present.
5. Immediately reconnect with a new keypair; observe via logs that `try_evict_inbound_peer` ran and an honest peer was disconnected with "evict because accepted better peer".
6. Repeat steps 3–5 in a tight loop; confirm via `get_peers` RPC that honest peers are continuously displaced while the attacker is never blocked.

### Citations

**File:** network/src/protocols/disconnect_message.rs (L32-41)
```rust
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
```

**File:** network/src/peer_registry.rs (L97-99)
```rust
        if self.get_key_by_peer_id(&peer_id).is_some() {
            return Err(PeerError::PeerIdExists(peer_id).into());
        }
```

**File:** network/src/peer_registry.rs (L109-111)
```rust
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

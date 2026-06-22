### Title
Missing `ban_session()` on Unsolicited/Wrong-Nonce Pong Allows Unbounded Reconnect Cycling — (`network/src/protocols/ping.rs`)

### Summary

The `PingHandler::received` Pong branch disconnects a peer that sends an unsolicited or wrong-nonce Pong via `async_disconnect_with_message`, but never calls `ban_session()`. Because no ban entry is written, the attacker's address is not added to the peer store's ban list, and the attacker can reconnect immediately and repeat the cycle indefinitely.

### Finding Description

In `PingHandler::received`, when a `Pong` arrives, the handler checks:

```rust
if let Some(status) = self.connected_session_ids.get_mut(&session.id)
    && (true, nonce) == (status.processing, status.nonce())
``` [1](#0-0) 

A newly connected peer has `processing = false` and `nonce = 0`. [2](#0-1) 

Sending any `Pong` before a `Ping` is issued makes the condition `(true, nonce) == (false, 0)` false, so execution falls through to:

```rust
if let Err(err) = async_disconnect_with_message(
    context.control(),
    session.id,
    "ping failed",
).await { ... }
``` [3](#0-2) 

`async_disconnect_with_message` only closes the session. It does **not** call `ban_session()`, which is the function that writes the peer's address to the ban list and prevents reconnection. [4](#0-3) 

A grep of `ping.rs` confirms zero calls to `ban_session`, `ban_peer`, or `report_session` anywhere in the file. By contrast, every other misbehavior handler in the codebase — `identify`, `hole_punching`, `sync` — calls `ban_session()` for protocol violations. [5](#0-4) 

### Impact Explanation

The victim node has a finite `max_inbound` connection limit enforced by `PeerRegistry`. [6](#0-5) 

An attacker cycling connect → `Pong(any)` → disconnect → reconnect occupies and releases inbound slots at a rate bounded only by the TCP + Secio handshake cost. Because no ban entry is ever written, the attacker's IP is never blocked, and the cycle can continue indefinitely. Sustained cycling keeps inbound slots transiently occupied, degrading the node's ability to accept legitimate peers needed for block and transaction propagation.

### Likelihood Explanation

The attack requires only a standard P2P connection and a single malformed message per cycle. No privileged access, leaked keys, or majority hashpower is needed. The Secio handshake imposes a per-cycle cost on the attacker, which limits the raw rate, but a single machine can sustain tens of cycles per second — sufficient to keep a node's inbound slots (typically 125 by default) under continuous pressure.

### Recommendation

After the nonce-mismatch or unsolicited-pong branch, call `ban_session()` (or at minimum `report_session()` with a `Behaviour::UnexpectedMessage` penalty) before or instead of `async_disconnect_with_message`, consistent with how `identify`, `hole_punching`, and `sync` handle protocol violations.

### Proof of Concept

```
for i in 1..=1000:
    connect to victim via TCP + Secio
    wait for ping protocol `connected` callback
    send PingMessage { payload: Pong { nonce: 0xdeadbeef } }
    observe disconnect (no ban entry written)
    assert victim's ban list is empty for attacker IP
    immediately reconnect
```

At each iteration the victim processes a full handshake and a protocol message, consuming CPU and an inbound slot, while the attacker pays no lasting penalty.

### Citations

**File:** network/src/protocols/ping.rs (L166-172)
```rust
        self.connected_session_ids
            .entry(session.id)
            .or_insert_with(|| PingStatus {
                last_ping_sent_at: Instant::now(),
                processing: false,
                nonce: 0,
            });
```

**File:** network/src/protocols/ping.rs (L227-234)
```rust
                        if let Some(status) = self.connected_session_ids.get_mut(&session.id)
                            && (true, nonce) == (status.processing, status.nonce())
                        {
                            status.processing = false;
                            let last_ping_sent_at = status.last_ping_sent_at;
                            self.pong_received(session.id, last_ping_sent_at);
                            return;
                        }
```

**File:** network/src/protocols/ping.rs (L236-244)
```rust
                        if let Err(err) = async_disconnect_with_message(
                            context.control(),
                            session.id,
                            "ping failed",
                        )
                        .await
                        {
                            debug!("Disconnect failed {:?}, error: {:?}", session.id, err);
                        }
```

**File:** network/src/network.rs (L241-274)
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
```

**File:** network/src/protocols/hole_punching/mod.rs (L83-89)
```rust
                self.network_state.ban_session(
                    &context.control().clone().into(),
                    session_id,
                    BAD_MESSAGE_BAN_TIME,
                    String::from("send us a malformed message"),
                );
                return;
```

**File:** network/src/peer_registry.rs (L24-26)
```rust
    // max inbound limitation
    max_inbound: u32,
    // max outbound limitation
```

Audit Report

## Title
Hole-Punching Cooldown Bypass via Post-Send State Update on Failed Async Send — (File: `network/src/protocols/hole_punching/component/connection_request.rs`)

## Summary
In `respond_delivered`, the `pending_delivered` cooldown entry is inserted only after a successful `send_message_to` call. If the send fails because the requesting peer has already closed the TCP connection, the function returns early via `StatusCode::ForwardError` and `pending_delivered` is never updated. Because the cooldown check at the top of `respond_delivered` finds no entry for `from_peer_id`, the 2-minute `HOLE_PUNCHING_INTERVAL` is never enforced, allowing a peer to repeatedly trigger the full response-building path.

## Finding Description
The control flow in `respond_delivered` (lines 155–240 of `connection_request.rs`) is:

1. **Cooldown check** (L161–167): reads `pending_delivered` for `from_peer_id`; if no entry exists, proceeds unconditionally.
2. **Response construction** (L168–224): collects up to 24 public/observed addresses, builds a `ConnectionRequestDelivered` packed message.
3. **Async send** (L226–232): calls `send_message_to(self.peer, proto_id, new_message).await`; on error, returns `ForwardError` immediately.
4. **State update** (L234–237): inserts `(remote_listens, now)` into `pending_delivered` — only reached on success. [1](#0-0) [2](#0-1) 

If the attacker closes the TCP connection after sending the `ConnectionRequest`, step 3 returns `Err`, step 4 is never reached, and `pending_delivered` remains empty for `from_peer_id`. On the next reconnect, step 1 finds no entry and the full path executes again.

The two rate limiters do not compensate:

- `rate_limiter` is keyed by `(PeerIndex, u32)`. A new `PeerIndex` is assigned on each new session, so reconnecting resets this limiter entirely. [3](#0-2) [4](#0-3) 

- `forward_rate_limiter` is keyed by `(content.from, content.to, msg_item_id)`. The `msg_item_id` is the union discriminant — always `0` for `ConnectionRequest` and not user-controllable. However, `content.from` is the claimed peer ID from the message body, which the attacker fully controls. By using a fresh, attacker-generated `from_peer_id` on each reconnect, the attacker produces a fresh key in `forward_rate_limiter`, bypassing it entirely. Note: the report's claim that `item_id` is user-incrementable is factually incorrect (it is a fixed union discriminant), but the bypass via rotating `from_peer_id` is equally trivial and achieves the same result. [5](#0-4) 

`HOLE_PUNCHING_INTERVAL` is 2 minutes. [6](#0-5) 

## Impact Explanation
Each bypass iteration forces the victim node to query and serialize up to 24 public/observed addresses, build a packed `ConnectionRequestDelivered` message, and perform an async send that fails. Because no cooldown is ever recorded, this can be repeated at the rate of TCP reconnections with no upper bound imposed by the protocol. This constitutes a low-cost, high-repetition resource exhaustion attack against a targeted CKB node. **High — Vulnerabilities or bad designs which could cause CKB network congestion with few costs** (10001–15000 points).

## Likelihood Explanation
Any peer reachable over the P2P network can trigger this. No special privileges, cryptographic keys, or stake are required. The only cost per iteration is one TCP handshake and one protocol message. The attack is directly reachable from the `received` handler, which is exposed to all connected peers. Bypassing `forward_rate_limiter` via rotating `from_peer_id` values is trivial in any custom P2P client. [7](#0-6) 

## Recommendation
Apply the checks-effects-interactions pattern: insert into `pending_delivered` **before** calling `send_message_to`. This ensures the cooldown is enforced regardless of whether the send succeeds or fails:

```rust
// Insert BEFORE the external call
let now = unix_time_as_millis();
self.protocol
    .pending_delivered
    .insert(from_peer_id.clone(), (remote_listens.clone(), now));

if let Err(error) = self
    .p2p_control
    .send_message_to(self.peer, proto_id, new_message)
    .await
{
    // Cooldown is already recorded; early return is safe
    return StatusCode::ForwardError.with_context(error);
}
``` [8](#0-7) 

## Proof of Concept
1. Attacker peer connects to victim node over TCP.
2. Attacker sends a `ConnectionRequest` with `to = victim_peer_id`, a fresh attacker-generated `from_peer_id`, and valid TCP `listen_addrs`.
3. Attacker immediately closes the TCP connection.
4. Victim's `respond_delivered` passes the `pending_delivered` check (no entry for this `from_peer_id`), builds the `ConnectionRequestDelivered` response, calls `send_message_to` — which returns `Err` because the session is gone.
5. `respond_delivered` returns `ForwardError`; `pending_delivered` is **not** updated.
6. Attacker reconnects (new session, new `PeerIndex`), sends `ConnectionRequest` with a new `from_peer_id` (fresh key in both `pending_delivered` and `forward_rate_limiter`).
7. Both cooldown and rate limiter checks pass again — repeat from step 3.

Each iteration forces the victim to rebuild and attempt to send a `ConnectionRequestDelivered` message. The 2-minute cooldown never takes effect. A script looping TCP connect → send → disconnect sustains continuous resource consumption on the victim with no protocol-level enforcement stopping it.

### Citations

**File:** network/src/protocols/hole_punching/component/connection_request.rs (L132-143)
```rust
        if self
            .protocol
            .forward_rate_limiter
            .check_key(&(content.from.clone(), content.to.clone(), self.msg_item_id))
            .is_err()
        {
            debug!(
                "from: {}, to {}, item_name: {}, rate limit is reached",
                content.from, content.to, "ConnectionRequest",
            );
            return StatusCode::TooManyRequests.with_context("ConnectionRequest");
        }
```

**File:** network/src/protocols/hole_punching/component/connection_request.rs (L161-167)
```rust
        if let Some((_, t)) = self.protocol.pending_delivered.get(&from_peer_id) {
            let now = unix_time_as_millis();
            if now - t < HOLE_PUNCHING_INTERVAL {
                return StatusCode::Ignore
                    .with_context("a same message is already replied in a moment ago");
            }
        }
```

**File:** network/src/protocols/hole_punching/component/connection_request.rs (L226-239)
```rust
        if let Err(error) = self
            .p2p_control
            .send_message_to(self.peer, proto_id, new_message)
            .await
        {
            return StatusCode::ForwardError.with_context(error);
        }

        let now = unix_time_as_millis();
        self.protocol
            .pending_delivered
            .insert(from_peer_id, (remote_listens, now));

        Status::ok()
```

**File:** network/src/protocols/hole_punching/mod.rs (L24-24)
```rust
pub(crate) const HOLE_PUNCHING_INTERVAL: u64 = 2 * 60 * 1000; // 2 minutes
```

**File:** network/src/protocols/hole_punching/mod.rs (L45-46)
```rust
    rate_limiter: RateLimiter<(PeerIndex, u32)>,
    forward_rate_limiter: RateLimiter<(PeerId, PeerId, u32)>,
```

**File:** network/src/protocols/hole_punching/mod.rs (L66-69)
```rust
    async fn disconnected(&mut self, context: ProtocolContextMutRef<'_>) {
        self.rate_limiter.retain_recent();
        self.forward_rate_limiter.retain_recent();
        debug!("HolePunching.disconnected session={}", context.session.id);
```

**File:** network/src/protocols/hole_punching/mod.rs (L72-107)
```rust
    async fn received(&mut self, context: ProtocolContextMutRef<'_>, data: bytes::Bytes) {
        let session_id = context.session.id;
        trace!("HolePunching.received session={}", session_id);

        let msg = match packed::HolePunchingMessageReader::from_slice(&data) {
            Ok(msg) => msg.to_enum(),
            _ => {
                warn!(
                    "HolePunching.received a malformed message from {}",
                    session_id
                );
                self.network_state.ban_session(
                    &context.control().clone().into(),
                    session_id,
                    BAD_MESSAGE_BAN_TIME,
                    String::from("send us a malformed message"),
                );
                return;
            }
        };

        let item_name = msg.item_name();

        if self
            .rate_limiter
            .check_key(&(session_id, msg.item_id()))
            .is_err()
        {
            debug!(
                "process {} from {}; result is {}",
                item_name,
                session_id,
                status::StatusCode::TooManyRequests.with_context(msg.item_name())
            );
            return;
        }
```
